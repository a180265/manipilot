"""目录级测试夹具。

wizard-phone-watch 轮询线程泄漏归因（2026-09-30）：每个创建 Env/向导的用例
泄漏一个 0.1s 轮询线程，串行全量跑到 pose/wake 族时累计 17+ 线程与主线程
争抢 GIL——cv2.findContours 的 numpy 转换被系统性饿死，进程 100% CPU 零推进
（livelock，pytest-timeout 的 thread 方法也救不了：异常投递同样需要 GIL）。
本夹具在每个用例后 cancel 向导（_watcher_gen 自增使旧 watcher 自行退出）。
"""
import pytest


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
