"""L02 验收：模板不可变记录/草稿/资产 CAS 管理。

FIX-14①契约：已发布模板/资产 INSERT+唯一约束冲突检测——
同 ID+version 同内容摘要复用（幂等）、异内容 409 version_conflict；
资产 sha256 基于原字节；同 ID 异字节 409 asset_conflict。
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import pytest

from runtime.layout_template import (
    LayoutTemplateService,
    LayoutTemplateStore,
    TemplateError,
    collect_asset_ids,
    sha256_bytes,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-image-bytes-0001"


@pytest.fixture()
def svc(tmp_path):
    return LayoutTemplateService(LayoutTemplateStore(tmp_path / "layout.db"))


def test_publish_creates_immutable_version(svc):
    req = {"template_id": "tpl-1", "version": 1,
           "pages": {"list": {"anchors": [{"name": "back"}]}},
           "metadata": {"description": "主列表"}}
    result = svc.publish(req)
    assert result["template_id"] == "tpl-1" and result["version"] == 1
    stored = svc._store.get_template("tpl-1", 1)
    assert stored["pages"]["list"]["anchors"][0]["name"] == "back"
    # 不同版本可共存
    svc.publish({**req, "version": 2, "pages": {"list": {"anchors": []}}})
    assert svc._store.get_template("tpl-1", 2) is not None
    # v1 不受影响
    assert svc._store.get_template("tpl-1", 1)["pages"]["list"]["anchors"]


def test_publish_rejects_same_version_different_content(svc):
    req = {"template_id": "tpl-1", "version": 1,
           "pages": {"list": {"anchors": [{"name": "back"}]}}}
    svc.publish(req)
    # 同 ID+version 异内容：明确冲突，不可覆盖
    with pytest.raises(TemplateError) as exc:
        svc.publish({**req, "pages": {"list": {"anchors": [{"name": "x"}]}}})
    assert exc.value.code == "version_conflict"
    assert exc.value.status == 409
    # 原版本字节未被改写
    assert svc._store.get_template(
        "tpl-1", 1)["pages"]["list"]["anchors"][0]["name"] == "back"


def test_publish_same_content_is_idempotent_reuse(svc):
    req = {"template_id": "tpl-1", "version": 1,
           "pages": {"list": {"anchors": [{"name": "back"}]}}}
    first = svc.publish(req)
    second = svc.publish(dict(req))  # 同 ID+version 同内容 → 复用
    assert first["sha256"] == second["sha256"]
    assert second["reused"] is True
    assert svc._store.get_template("tpl-1", 1)["sha256"] == first["sha256"]


def test_draft_crud(svc):
    draft = svc.create_draft({"template_id": "tpl-1", "pages": {}})
    assert draft["draft_id"]
    got = svc.get_draft(draft["draft_id"])
    assert got is not None and got["pages"] == {}
    svc.update_draft_metadata(draft["draft_id"], {"desc": "更新"})
    assert svc.get_draft(draft["draft_id"])["metadata"]["desc"] == "更新"


def test_asset_upload_and_retrieve(svc):
    draft = svc.create_draft({"template_id": "tpl-1"})
    svc.upload_asset(draft["draft_id"], "asset-1", b"\x89PNG")
    assert svc.get_asset(draft["draft_id"], "asset-1") == b"\x89PNG"


def test_asset_sha256_is_over_raw_bytes_with_conflict_detection(svc):
    draft = svc.create_draft({"template_id": "tpl-1"})
    sha = svc.upload_asset(draft["draft_id"], "asset-1", PNG_BYTES)
    # 摘要基于原字节，而非 hex/JSON 替代摘要
    assert sha == sha256_bytes(PNG_BYTES)
    # 同 ID 同字节 → 复用幂等
    assert svc.upload_asset(draft["draft_id"], "asset-1",
                            PNG_BYTES) == sha
    # 同 ID 异字节 → 409 冲突，不原地替换
    with pytest.raises(TemplateError) as exc:
        svc.upload_asset(draft["draft_id"], "asset-1", PNG_BYTES + b"x")
    assert exc.value.code == "asset_conflict"
    assert exc.value.status == 409
    assert svc.get_asset(draft["draft_id"], "asset-1") == PNG_BYTES


def test_publish_copies_referenced_assets_from_draft(svc):
    draft = svc.create_draft({"template_id": "tpl-1"})
    svc.upload_asset(draft["draft_id"], "ref-1", PNG_BYTES)
    pages = {"wechat_list": {"anchors": [
        {"anchor_id": "a1", "empty_input_asset_ref": "ref-1"}]}}
    svc.publish({"template_id": "tpl-1", "version": 1, "pages": pages,
                 "draft_id": draft["draft_id"]})
    # 模板所有者名下可读到原字节（api get_template_asset 即此键）
    assert svc.get_asset("tpl-1", "ref-1") == PNG_BYTES
    assert svc._store.list_assets("tpl-1") == [
        {"asset_id": "ref-1", "sha256": sha256_bytes(PNG_BYTES),
         "size_bytes": len(PNG_BYTES)}]
    # 引用了不存在的资产 → 拒绝发布
    svc.upload_asset(draft["draft_id"], "ref-2", b"second")
    with pytest.raises(TemplateError, match="asset_missing"):
        svc.publish({"template_id": "tpl-1", "version": 2,
                     "pages": {"wechat_list": {"anchors": [
                         {"anchor_id": "a", "asset_ref": "ghost"}]}},
                     "draft_id": draft["draft_id"]})


def test_collect_asset_ids_walks_nested_definitions():
    pages = {"wechat_list": {"anchors": [{"empty_input_asset_ref": "r1"}],
                             "targets": [{"asset_id": "t1"}],
                             "regions": [{"asset": {"id": "g1"}}]}}
    assert collect_asset_ids(pages) == {"r1", "t1", "g1"}
