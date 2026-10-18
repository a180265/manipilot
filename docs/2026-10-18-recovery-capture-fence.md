# 恢复复核相机采样时刻门修复（2026-10-18）

恢复复核在机械臂移动后立即取图，可能读到序号已推进、但实际采样仍早于动作结束的画面。收到新帧与动作后采样是不同事实。此次修复已双向移植，普通单次取帧保持原行为。

`fetch_camera_frame` 增加可选 `not_before_monotonic_ns`。有门槛时最多等待三秒、最多读取 61 次；每次 HTTP 超时不超过剩余预算。仅当既有 `frame_chain_identity` 接受源采样证明，且 `captured_monotonic_ns - ceil(uncertainty_ms * 1_000_000)` 严格晚于门槛，才返回原始图像。缺失/非法证明立即拒绝；HTTP 返回时再次检查截止时刻。轮询等待本身不作为采样证明。

`_arm_anchor_verify` 的前图门槛在读取并确认安全悬停原点后记录，后图门槛在悬停走位返回后记录。既有探头识别、45 px 偏差门、会话核对和返回实际原点保持。该复核不触发 TAP；前图失败不移动，后图失败仍按同会话核对返回原点。

`HttpFrameSource.fetch_bytes` 现在透出本次已校验的源采样元数据。它不提前提交解码后的序号、代际退休或分辨率基线，失败解码与代际切换的既有护栏保持。

验证均使用假 HTTP 响应/假运动，不连接设备或生产服务：

- 首轮红测：原实现 8 failed / 1 passed，缺少门槛参数与原字节采样元数据。
- 最终新增文件 `tests/test_motor_debug_recovery_capture_fence.py`：13 项，涵盖旧采样新序号、采样误差区间、缺失/非法证明、有限期限、超时返回的新采样拒绝、普通取帧兼容、元数据与序号基线分离、前后图门槛及失败返回。
- WX：`tools/fleet_test.sh tests/test_motor_debug_recovery_capture_fence.py tests/test_http_frame_source_generations.py tests/test_motor_debug_serial_ports.py::test_arm_anchor_verify_bounded_and_deviation tests/test_motor_debug_multi_device.py::test_fetch_camera_frame_checks_camera_and_device_headers -q -p no:cacheprovider`，33 passed。
- SDK：相同测试加 `tests/test_platform_isolation.py`，使用 WX `.venv-wx/bin/pytest` 并清除三个 CAMERA 产线环境变量，38 passed。
- 两仓 `git diff --check` 通过。F821 仅原有四处 `body` 未定义；两仓各用 HEAD 同环境检查复现，改动未增加未定义名。
- 同步检查 193 文件并集，本改动 `drivers/real_camera.py` 无漂移；其余四处正在并行修改的差异须由主线程最终复核（camera_calibration、calibration_auto_runner、screen_pose、screen_proof）。

现场验收由主线程记录：本报告不宣称硬件恢复已通过。加载此次修改需只刷新调试服务的 Python 进程，保留 daemon 与串口会话；服务控制与运行状态由主线程处理。本子任务未调用硬件、服务或网络接口，未修改运行数据与 Git index。
