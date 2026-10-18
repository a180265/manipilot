"""Base-template draft assets obey the same frozen response as uploaded assets."""
from __future__ import annotations

import base64
import copy
import uuid

import pytest

from runtime.layout_drafts import LayoutDraftService
from runtime.layout_state import LayoutStateStore, state_key
from runtime.layout_template import LayoutTemplateStore
from tests.layout_d2_helpers import load_contract, wire_fixtures


@pytest.mark.parametrize("legacy_flat_record", [False, True])
def test_base_template_draft_get_matches_frozen_asset_record(tmp_path, legacy_flat_record):
    fixtures = wire_fixtures()
    template = copy.deepcopy(fixtures["http-template_version-response"]["body"]["record"])
    response = copy.deepcopy(fixtures["http-draft_get-response"])
    base_ref = {"id": template["template_id"], "version": template["version"],
                "sha256": template["sha256"]}
    data = base64.b64decode(fixtures["http-draft_asset_upload-request"]["body"]["content_base64"])
    templates = LayoutTemplateStore(tmp_path / "layout.db")
    state = LayoutStateStore(tmp_path / "layout.db")
    try:
        templates.save_template(template["template_id"], template["version"], template)
        for asset in template["assets"]:
            templates.save_asset(template["template_id"], asset["id"], data)
        sample = response["body"]["draft"]
        svc = LayoutDraftService(templates, state,
            binding_snapshot_provider=lambda _: copy.deepcopy(sample["binding_snapshot"]))
        if legacy_flat_record:
            sample["base_template_ref"] = base_ref
            sample["assets"] = copy.deepcopy(template["assets"])
            draft_id = sample["draft_id"]
            state.state_insert("draft", state_key("draft", [draft_id]),
                               sample["device_id"], sample["principal_id"], sample)
            for asset in template["assets"]:
                templates.save_asset(draft_id, asset["id"], data)
        else:
            draft_id = svc.create({"device_id": sample["device_id"],
                "client_request_id": str(uuid.uuid4()), "base_template_ref": base_ref},
                sample["principal_id"])["draft_id"]
        result = svc.get(draft_id, sample["principal_id"])
        response["body"].update(result)
        load_contract().validate("wire", "draft_get_response", response)
        records = result["draft"]["assets"]
        assert [record["asset"] for record in records] == template["assets"]
        assert all(record["byte_size"] == len(data) for record in records)
        assert all(record["source_evidence_ids"] == [] for record in records)
        for record in records:
            assert templates.get_asset(draft_id, record["asset"]["id"]) == data
        assert templates.get_template(template["template_id"], template["version"]) == template
    finally:
        state.close()
        templates.close()
