# CameraHelper

CameraHelper 是 Arm Fleet 的 macOS 原生摄像头辅助程序。一个 App 可管理 cam-01 到 cam-10，每路使用独立 AVCaptureSession，将 JPEG 上传到本机 camera_frame_server.py。

源码入口：[main.swift](Sources/CameraHelper/main.swift)；帧服务入口：
[camera_frame_server.py](../../scripts/camera_frame_server.py)。本文描述组件职责和操作顺序，
不证明现场 App 已重建、获授权或绑定了可用摄像头。完整边界见[部署说明](../../docs/部署与环境变量.md)。

## 构建

~~~bash
cd arm_fleet/tools/camera_helper
./build_app.sh
~~~

产物：

~~~
dist/CameraHelper.app
~~~

## 运行顺序

1. 启动 scripts/camera_frame_server.py。
2. 打开 CameraHelper.app 并授权摄像头。
3. 点击“刷新摄像头”。
4. 每个 camera 通道选择对应物理摄像头，点击“保存绑定”（已启动的通道须先停止）。
5. 逐路启动，或点击“启动全部已绑定通道”。
6. 核对 `/cameras/{camera_id}/status` 为 `streaming`，再在调试台选择对应机械臂校准。

CameraHelper 只负责采集和上传画面（采集层），不控制机械臂，不修改校准档案；
界面识别（OCR）由 Arm Fleet 侧的 Apple Vision 适配器完成（识别层），两者严格分离。
所有上传帧带 `X-Frame-Source: camera_helper` 正式来源标记；Chrome 兼容页帧带
`chrome_compat` 降级标记，两者不允许同时写同一通道。

## 窗口行为

- 关闭窗口（红点）＝停止全部采集推流：进程留在程序坞，点程序坞图标（或再
  `open dist/CameraHelper.app`）即唤回窗口，不必整体退出重开。
- 彻底退出用 ⌘Q 或 `osascript -e 'quit app "CameraHelper"'`（启停台「停止」即此命令）。

## 断线与权限行为

- macOS 未授权/拒绝摄像头权限：状态栏给出中文指引，不采集。
- UVC 摄像头拔出：对应通道停止并提示中文处理建议，其余通道不受影响；
  重新插拔后需人工「刷新摄像头」并重新启动该通道，恢复后须重新确认绑定。
- 摄像头被其他程序占用：该通道启动失败并给出中文原因，不影响其他通道。

## 通道事件审计

帧服务（8790）按通道记录绑定变更、来源切换/冲突、分辨率不匹配、画面过期/恢复
与服务重启事件：`GET /cameras/{camera_id}/events`，并追加落盘
`data/camera_helper/events.jsonl`。
