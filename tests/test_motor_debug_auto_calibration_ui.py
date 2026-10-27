"""Run the real calibration browser handlers through Node's DOM-free harness."""

import pathlib
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("script_name", [
    "motor_debug_auto_calibration_ui.test.cjs",
    "motor_debug_gesture_ui.test.cjs",
    "motor_debug_camera_mapping_ui.test.cjs",
])
def test_calibration_ui_handlers(script_name):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the calibration UI handler regression")
    result = subprocess.run(
        [node, "--test", str(pathlib.Path(__file__).with_name(script_name))],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
