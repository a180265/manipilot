"""输入开关持久化/并发/幂等/损坏保护单元测试（规格 §4.1/§4.2/§9.1 I04/I05/I16/I17）。"""
import json
import sys
import threading

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from runtime import input_settings as iset
from runtime.input_settings import InputSettingsError


def test_missing_file_defaults_off_i04(tmp_path):
    settings = iset.load_settings("arm-01", tmp_path)
    assert settings["enabled"] is False and settings["revision"] == 0
    assert iset.is_enabled("arm-01", tmp_path) is False
    # 不从旧 E 通过记录推断：即使 bundle 有 debug=true 也不影响（本模块不读 bundle）


def test_enable_requires_revision_and_calibration_i08(tmp_path):
    with pytest.raises(InputSettingsError) as e:
        iset.update_settings("arm-01", data_root=tmp_path, enabled=True,
                             ime_device_id="k40-1", expected_revision=0,
                             operator_id="操作员", reason="允许",
                             client_request_id="c1", calibration_ready=False)
    assert e.value.reason_code == "calibration_not_ready"
    # 校准就绪后可开
    result = iset.update_settings("arm-01", data_root=tmp_path, enabled=True,
                                  ime_device_id="k40-1", expected_revision=0,
                                  operator_id="操作员", reason="允许",
                                  client_request_id="c1", calibration_ready=True)
    assert result["settings"]["effective"] is True and result["replayed"] is False
    stored = iset.load_settings("arm-01", tmp_path)
    assert stored["enabled"] is True and stored["revision"] == 1
    assert stored["ime_device_id"] == "k40-1"


def test_close_works_even_without_calibration_i08(tmp_path):
    iset.update_settings("arm-01", data_root=tmp_path, enabled=True,
                         ime_device_id="k40-1", expected_revision=0,
                         operator_id="操作员", reason="开",
                         client_request_id="open", calibration_ready=True)
    result = iset.update_settings("arm-01", data_root=tmp_path, enabled=False,
                                  ime_device_id="k40-1", expected_revision=1,
                                  operator_id="操作员", reason="关",
                                  client_request_id="close", calibration_ready=False)
    assert result["settings"]["effective"] is False
    assert iset.load_settings("arm-01", tmp_path)["enabled"] is False


def test_close_allows_null_ime_device(tmp_path):
    iset.update_settings("arm-01", data_root=tmp_path, enabled=True,
                         ime_device_id="k40-1", expected_revision=0,
                         operator_id="操作员", reason="开",
                         client_request_id="open", calibration_ready=True)
    # §4.2.1/§4.7：关闭允许省略/失效的旧手机编号，只关闭路径设备
    result = iset.update_settings("arm-01", data_root=tmp_path, enabled=False,
                                  ime_device_id=None, expected_revision=1,
                                  operator_id="操作员", reason="关",
                                  client_request_id="close", calibration_ready=False)
    assert result["replayed"] is False
    stored = iset.load_settings("arm-01", tmp_path)
    # R2-02 更正（规格 §4.1）：关闭保留原授权手机——请求省略手机时保存值
    # 不再清空（第一轮断言按旧实现写的，随新需求修订）。
    assert stored["enabled"] is False and stored["ime_device_id"] == "k40-1"


def test_revocation_boundary_tracks_close_and_replay_keeps_snapshot(tmp_path):
    """§4.5.2 顺序：开(r1,边界0) → 关(r2,边界2) → 开(r3,边界仍2)；重放开=原快照。"""
    iset.update_settings("arm-01", data_root=tmp_path, enabled=True,
                         ime_device_id="k40-1", expected_revision=0,
                         operator_id="a", reason="开",
                         client_request_id="open-1", calibration_ready=True)
    iset.update_settings("arm-01", data_root=tmp_path, enabled=False,
                         ime_device_id=None, expected_revision=1,
                         operator_id="a", reason="关",
                         client_request_id="close-1", calibration_ready=False)
    third = iset.update_settings("arm-01", data_root=tmp_path, enabled=True,
                                 ime_device_id="k40-1", expected_revision=2,
                                 operator_id="a", reason="再开",
                                 client_request_id="open-2", calibration_ready=True)
    assert third["settings"]["revision"] == 3
    stored = iset.load_settings("arm-01", tmp_path)
    assert stored["revoked_through_revision"] == 2, "开启不降低撤销边界"
    # 重放 open-1：返回原快照（revision=1、原观察 effective=true），当前值不被覆盖
    replay = iset.update_settings("arm-01", data_root=tmp_path, enabled=True,
                                  ime_device_id="k40-1", expected_revision=0,
                                  operator_id="a", reason="开",
                                  client_request_id="open-1", calibration_ready=True)
    assert replay["replayed"] is True
    assert replay["settings"]["revision"] == 1
    assert replay["settings"]["effective"] is True
    assert iset.load_settings("arm-01", tmp_path)["revision"] == 3


def test_permission_denied_precedes_replay(tmp_path):
    iset.update_settings("arm-01", data_root=tmp_path, enabled=True,
                         ime_device_id="k40-1", expected_revision=0,
                         operator_id="a", reason="开",
                         client_request_id="open-1", calibration_ready=True)
    # §4.1/§4.7：被撤权身份重放同一编号也必须先拒绝，不能借重放取回结果
    with pytest.raises(InputSettingsError) as e:
        iset.update_settings("arm-01", data_root=tmp_path, enabled=True,
                             ime_device_id="k40-1", expected_revision=0,
                             operator_id="a", reason="开",
                             client_request_id="open-1", calibration_ready=True,
                             permission_denied="wx 不可写")
    assert e.value.reason_code == "permission_denied"


def test_idempotent_replay_and_conflict_i16(tmp_path):
    iset.update_settings("arm-01", data_root=tmp_path, enabled=True,
                         ime_device_id="k40-1", expected_revision=0,
                         operator_id="操作员", reason="开",
                         client_request_id="same-id", calibration_ready=True)
    before = iset.load_settings("arm-01", tmp_path)["revision"]
    # 同编号同内容重放：返回原结果，版本不再递增
    replay = iset.update_settings("arm-01", data_root=tmp_path, enabled=True,
                                  ime_device_id="k40-1", expected_revision=0,
                                  operator_id="操作员", reason="开",
                                  client_request_id="same-id", calibration_ready=True)
    assert replay["replayed"] is True and replay["settings"]["revision"] == 1
    assert iset.load_settings("arm-01", tmp_path)["revision"] == before == 1
    # 同编号不同内容：冲突
    with pytest.raises(InputSettingsError):
        iset.update_settings("arm-01", data_root=tmp_path, enabled=False,
                             ime_device_id="k40-1", expected_revision=0,
                             operator_id="操作员", reason="改",
                             client_request_id="same-id", calibration_ready=True)
    # 旧 revision 写：409 语义
    with pytest.raises(InputSettingsError) as e:
        iset.update_settings("arm-01", data_root=tmp_path, enabled=False,
                             ime_device_id="k40-1", expected_revision=0,
                             operator_id="操作员", reason="旧版本",
                             client_request_id="stale", calibration_ready=True)
    assert e.value.reason_code == "revision_conflict"


def test_type_validation_i16(tmp_path):
    for bad in ("true", 1, None):
        with pytest.raises(InputSettingsError, match="布尔"):
            iset.update_settings("arm-01", data_root=tmp_path, enabled=bad,
                                 ime_device_id="k40-1", expected_revision=0,
                                 operator_id="操作员", reason="r",
                                 client_request_id="x", calibration_ready=True)



def test_corrupt_file_blocks_and_is_preserved_i17(tmp_path):
    iset.update_settings("arm-01", data_root=tmp_path, enabled=True,
                         ime_device_id="k40-1", expected_revision=0,
                         operator_id="操作员", reason="开",
                         client_request_id="c0", calibration_ready=True)
    path = iset.settings_path("arm-01", tmp_path)
    path.write_text("{corrupt", encoding="utf-8")
    corrupted = path.read_bytes()
    with pytest.raises(InputSettingsError) as e:
        iset.load_settings("arm-01", tmp_path)
    assert e.value.reason_code == "input_settings_invalid"
    assert iset.is_enabled("arm-01", tmp_path) is False
    # 更新尝试也不得清空/覆盖损坏文件
    with pytest.raises(InputSettingsError):
        iset.update_settings("arm-01", data_root=tmp_path, enabled=False,
                             ime_device_id="k40-1", expected_revision=1,
                             operator_id="操作员", reason="尝试写",
                             client_request_id="x", calibration_ready=True)
    assert path.read_bytes() == corrupted, "损坏文件不得被清空覆盖"


def test_two_devices_isolated(tmp_path):
    iset.update_settings("arm-01", data_root=tmp_path, enabled=True,
                         ime_device_id="k40-1", expected_revision=0,
                         operator_id="a", reason="r",
                         client_request_id="c1", calibration_ready=True)
    assert iset.is_enabled("arm-01", tmp_path) is True
    assert iset.is_enabled("arm-02", tmp_path) is False


def test_concurrent_writes_are_serialized_i15(tmp_path):
    errors: list[str] = []
    results: list[bool] = []

    def worker(index: int):
        try:
            # 每个 worker 以自身 revision 预期写；冲突方报 revision_conflict
            current = iset.load_settings("arm-01", tmp_path)["revision"]
            result = iset.update_settings(
                "arm-01", data_root=tmp_path, enabled=index % 2 == 0,
                ime_device_id="k40-1", expected_revision=current,
                operator_id=f"op{index}", reason="并发",
                client_request_id=f"conc-{index}", calibration_ready=True)
            results.append(result["settings"]["enabled"])
        except InputSettingsError as exc:
            errors.append(exc.reason_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stored = iset.load_settings("arm-01", tmp_path)
    assert stored["revision"] >= 1
    # 每个并发写要么成功要么明确 revision_conflict，不能出现别的错误
    assert all(code in {"revision_conflict"} for code in errors) or not errors
