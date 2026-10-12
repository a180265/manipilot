"""屏幕键盘逐字打字（screen_type）测试：布局模板、坐标换算、插件端到端。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from adapters.screen_ime.geometry import (
    ScreenRobotTransform,
    fit_affine,
)
from adapters.screen_ime.keyboard_layout import (
    IOS_QWERTY_26KEY_SPEC,
    KeyboardLayout,
    KeyboardLayoutError,
)
from adapters.screen_ime.service import (
    ScreenImeService,
    get_default_service,
    set_default_service,
)
from runtime.config import PlatformConfig
from runtime.runtime import ArmFleetRuntime
from runtime.util import new_id
from tests.helpers import make_device_configs, op_context
from tests.test_screen_ime import (
    PLUGIN_SOURCE,
    _service,
    _wait_terminal,
)


# ---------------------------------------------------------------------------
# 布局模板
# ---------------------------------------------------------------------------

def test_builtin_qwerty_layout_centers_are_inside_region():
    layout = KeyboardLayout.builtin_qwerty()
    assert layout.layout_id == IOS_QWERTY_26KEY_SPEC["layout_id"]
    region = layout.region
    for char, (u, v) in layout.keys.items():
        assert region["u0"] <= u <= region["u1"], char
        assert region["v0"] <= v <= region["v1"], char
    # 行序：第一行 q 在最上，最后一行 SPACE 在最下
    assert layout.keys["q"][1] < layout.keys["a"][1] < layout.keys["z"][1]
    # QWERTY 行内顺序
    assert layout.keys["q"][0] < layout.keys["p"][0]
    assert layout.keys["a"][0] < layout.keys["l"][0]
    # 宽键 SPACE 居中
    su, _ = layout.special["SPACE"]
    assert 0.4 < su < 0.6


def test_layout_plan_handles_shift_space_return_and_unknown():
    layout = KeyboardLayout.builtin_qwerty()
    plan = layout.plan_for_text("Hi a")
    kinds = [(a["kind"], a["key"]) for a in plan]
    # H：shift + h；i：i；空格：SPACE；a：a
    assert ("shift", "SHIFT") in kinds
    assert ("tap", "h") in kinds and ("tap", "i") in kinds
    assert ("tap", "SPACE") in kinds
    assert kinds == [("shift", "SHIFT"), ("tap", "h"), ("tap", "i"),
                     ("tap", "SPACE"), ("tap", "a")]
    with pytest.raises(KeyboardLayoutError) as exc:
        layout.plan_for_text("中")
    assert exc.value.code == "layout_unknown_char"
    with pytest.raises(KeyboardLayoutError):
        layout.center_for("中")


def test_layout_from_inline_spec_with_custom_region():
    spec = {
        "layout_id": "custom-24",
        "region": {"u0": 0.05, "v0": 0.6, "u1": 0.95, "v1": 0.95},
        "rows": [
            [["1", 1], ["2", 1], ["3", 1], ["4", 1], ["5", 1], ["6", 1], ["7", 1], ["8", 1]],
            [["q", 2], ["w", 2], ["e", 2], ["r", 2], ["t", 2], ["y", 2], ["z", 2]],
            [["SPACE", 5], ["RETURN", 3]],
        ],
    }
    layout = KeyboardLayout.from_spec(spec)
    u1, v1 = layout.keys["1"]
    u8, v8 = layout.keys["8"]
    assert u1 < u8 and v1 == v8  # 同一行：8 在 1 右侧
    su, sv = layout.special["SPACE"]
    assert spec["region"]["u0"] < su < spec["region"]["u1"]
    assert sv > v1  # SPACE 在最后一行


# ---------------------------------------------------------------------------
# 插件端到端：先校准拿映射，再 screen_type 逐键打字
# ---------------------------------------------------------------------------

def _runtime_with_plugin_installed(tmp_path: Path) -> ArmFleetRuntime:
    config = PlatformConfig(
        {"db_path": str(tmp_path / "fleet.db"), "evidence_dir": str(tmp_path / "evidence"),
         "plugin_dir": str(tmp_path / "plugins"), "hardware_mode": "simulation"},
        root=tmp_path,
    )
    runtime = ArmFleetRuntime(config)
    source = tmp_path / "screen-ime-src"
    source.mkdir(exist_ok=True)
    (source / "manifest.json").write_text(
        (PLUGIN_SOURCE / "manifest.json").read_text(encoding="utf-8"), encoding="utf-8")
    (source / "plugin.py").write_text(
        (PLUGIN_SOURCE / "plugin.py").read_text(encoding="utf-8"), encoding="utf-8")
    runtime.plugins.install(source, operator_id="操作员甲", reason="安装屏幕输入适配插件")
    runtime.plugins.activate("screen-ime-adapter")
    set_default_service(_service(tmp_path))
    return runtime



def test_plugin_screen_type_end_to_end(tmp_path):
    """先跑校准任务生成映射，再提交 screen_type：机械臂按布局逐键点击，坐标可逆推验证。"""

    async def scenario():
        runtime = _runtime_with_plugin_installed(tmp_path)
        await runtime.startup(device_configs=make_device_configs(1))
        from tests.helpers import enable_input_switch
        enable_input_switch(runtime, tmp_path)
        service = get_default_service()
        try:
            # 1) 屏幕校准（复用已有端到端流程：phone 双方与真实仿射）
            def true_screen(rx, ry):
                return (2.0 * rx + 540.0, 2.0 * ry + 1140.0)

            def robot_point(sequence):
                return (150.0 + (15.0 if sequence == 2 else 0.0),
                        60.0 + (15.0 if sequence == 3 else 0.0))

            async def phone():
                await service.tasks.heartbeat(
                    "k40-01", {"appVersion": "sim-ime", "keyboardActive": True})
                deadline = asyncio.get_running_loop().time() + 15
                while asyncio.get_running_loop().time() < deadline:
                    session = await service.calibrations.current("k40-01")
                    if session is not None:
                        if session["clientState"] != "open":
                            await service.calibrations.client(
                                session["sessionId"], "k40-01", "open")
                        armed = session["armed"]
                        if armed is not None:
                            rx, ry = robot_point(armed["sequence"])
                            sx, sy = true_screen(rx, ry)
                            await service.calibrations.touch(
                                session["sessionId"],
                                {"deviceId": "k40-01", "sampleId": armed["sampleId"],
                                 "sequence": armed["sequence"], "screenX": sx,
                                 "screenY": sy, "screenWidth": 1080,
                                 "screenHeight": 2400, "orientation": "portrait",
                                 "eventTime": 1})
                    await asyncio.sleep(0.02)

            phone_task = asyncio.create_task(phone())
            cal_submit = runtime.submit_task(
                {"task_type": "screen_calibration", "idempotency_key": f"cal-{new_id('k')}",
                 "device_id": "arm-01",
                 "payload": {"ime_device_id": "k40-01", "wait_seconds": 8},
                 "correlation_id": "corr-cal"},
                source="console", context=op_context())
            cal_task = await _wait_terminal(runtime, cal_submit["task_id"], timeout=15)
            await phone_task
            assert cal_task["status"] == "confirmed", cal_task.get("result_message")
            transform = ScreenRobotTransform.from_dict(service.mappings.get("k40-01"))

            # 2) 提交 screen_type：Hi a（含大写 → shift+h, i, 空格, a = 5 次点击）
            layout = KeyboardLayout.builtin_qwerty()
            text = "Hi a"
            expected = layout.plan_for_text(text)
            driver = runtime.devices.driver("arm-01")
            before = len(driver.action_log)

            submit = runtime.submit_task(
                {"task_type": "screen_type", "idempotency_key": f"type-{new_id('k')}",
                 "device_id": "arm-01",
                 "payload": {"text": text, "ime_device_id": "k40-01", "pace_ms": 100},
                 "correlation_id": "corr-type"},
                source="console", context=op_context())
            task = await _wait_terminal(runtime, submit["task_id"], timeout=25)
            assert task["status"] == "confirmed", task.get("result_message")
            assert task["status_display"] == "已输入"

            # 3) 点击次数 = 动作数；坐标 = 布局键位像素经逆变换的机械坐标
            clicks = [a for a in driver.action_log[before:] if a["action"] == "click"]
            assert len(clicks) == len(expected)
            width, height = 1080.0, 2400.0
            for click, action in zip(clicks, expected):
                px, py = action["u"] * width, action["v"] * height
                rx, ry = transform.robot_from_screen(px, py)
                params = click["params"]
                assert abs(params["robot_x"] - rx) < 1e-6, (click, rx, ry)
                assert abs(params["robot_y"] - ry) < 1e-6
        finally:
            await runtime.shutdown()
            set_default_service(None)

    asyncio.run(scenario())


def test_plugin_screen_type_requires_mapping(tmp_path):
    """没有校准映射时明确失败（mapping_not_found），不执行任何点击。"""

    async def scenario():
        runtime = _runtime_with_plugin_installed(tmp_path)
        await runtime.startup(device_configs=make_device_configs(1))
        from tests.helpers import enable_input_switch
        enable_input_switch(runtime, tmp_path)
        try:
            submit = runtime.submit_task(
                {"task_type": "screen_type", "idempotency_key": f"type-{new_id('k')}",
                 "device_id": "arm-01",
                 "payload": {"text": "hi", "ime_device_id": "k40-01"},
                 "correlation_id": "corr-type"},
                source="console", context=op_context())
            task = await _wait_terminal(runtime, submit["task_id"], timeout=15)
            assert task["status"] == "failed"
            assert task["error_category"] == "mapping_not_found"
            driver = runtime.devices.driver("arm-01")
            assert not [a for a in driver.action_log if a["action"] == "click"]
        finally:
            await runtime.shutdown()
            set_default_service(None)

    asyncio.run(scenario())
