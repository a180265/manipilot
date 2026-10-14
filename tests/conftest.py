"""目录级测试夹具。

wizard-phone-watch 轮询线程泄漏归因（2026-09-30）：每个创建 Env/向导的用例
泄漏一个 0.1s 轮询线程，串行全量跑到 pose/wake 族时累计 17+ 线程与主线程
争抢 GIL——cv2.findContours 的 numpy 转换被系统性饿死，进程 100% CPU 零推进
（livelock，pytest-timeout 的 thread 方法也救不了：异常投递同样需要 GIL）。
本夹具在每个用例后 cancel 向导（_watcher_gen 自增使旧 watcher 自行退出）。

产线摄像头 env 隔离（2026-10-14 复盘 A 项）：launchd/手工起过帧桥后，
CAMERA_CONTROL_TOKEN_FILE / CAMERA_HOST_INSTALLATION_ID_FILE /
CAMERA_HELPER_EXEC 常驻全局环境。必须在本文件**模块顶层**删除（conftest
先于全部测试模块导入）：scripts.camera_frame_server 在导入时就把
`V2_ENABLED = CONTROL_TOKEN_FILE is not None` 固化成模块全局，autouse
夹具在导入后清理已来不及——曾致 4 文件 27 例假红（source_timing_unproven
/ camera_permission_denied / v2 绑定写入口已关闭）。测试要这些变量时用
monkeypatch.setenv 显式给值。
"""
import os

for _var in ("CAMERA_CONTROL_TOKEN_FILE",
             "CAMERA_HOST_INSTALLATION_ID_FILE",
             "CAMERA_HELPER_EXEC"):
    os.environ.pop(_var, None)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _stop_wizard_watchers():
    from tests.test_calibration_wizard import WIZARD_ENVS

    yield
    while WIZARD_ENVS:
        env = WIZARD_ENVS.pop()
        try:
            env.close()
        except Exception:
            pass
