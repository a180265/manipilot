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

## 固定采集格式

程序按物理摄像头 `uniqueID` 保存完整输入格式，避免重启后自动协商出不同媒体子类型而使来源几何变化。
未登记的设备首次仍按原有 720p preset 启动；启动后读取设备和输入端口的实际格式，两者一致才保存。
这只记录采集事实，不证明任何旧校准可用；旧校准仍须通过来源几何和绑定校验。
以后启动精确选择已保存格式；设备不支持、配置损坏或启动后读回不符均停止并明确报错，不能自动换格式。
macOS 不支持 `inputPriority`，因此固定模式通过配置事务和设备锁设置 `activeFormat`，不设置帧率。
先固定物理格式再创建输入端口，避免输入仍保留创建时的旧媒体类型。设备配置锁必须保留整个采集期；提前解锁会允许 session 按 preset 重新选择格式。正常停止或启动失败时，先停止 session 再释放锁。

偏好存于 `com.armfleet.camerahelper` 域的 `captureFormatPinsByUniqueID` 字典。
正式 App 通过 `UserDefaults.standard` 访问自身域；CLI/异域进程显式打开该 suite，打开失败即报错，不能当作空配置。若需恢复已有校准对应的格式，
先停止通道并退出 App，再按绑定中的物理 `uniqueID` 配置。下面仅为完整格式示例，须与该设备实际支持的格式和校准一致：

~~~bash
defaults write com.armfleet.camerahelper captureFormatPinsByUniqueID -dict-add \
  '<物理摄像头 uniqueID>' 'fourcc:79757673;size:1920x1080;pixel-aspect:1/1'
defaults read com.armfleet.camerahelper captureFormatPinsByUniqueID
~~~

重新打开 App 并启动通道后，核对帧服务实际 `format_id`、`source_geometry_id` 和已有校准。
更换摄像头或主动改格式后，原校准不匹配时需要重新校准，不能更改摘要绕过校验。
离线配置/持久化/精确选择/启动读回拒绝检查：`./test_capture_format_pins.sh`；不会打开摄像头。
格式锁生命周期/会话重新协商检查：`python3 Tests/check_format_lock_lifetime.py`，提取实际配置与清理方法配假设备重放；不打开摄像头。
启动成功/失败状态顺序检查：`python3 Tests/check_channel_start_status.py`，提取实际界面方法配假通道重放；不启动 GUI 或摄像头。

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

保存绑定失败时显示 HTTP 状态、服务端中文原因和错误码；长消息可悬停查看。
物理摄像头可独立绑定到 cam-01～cam-10，不以机械臂在线或机械状态已知为前提。
`camera_unique_id_conflict` 表示相机已被其他通道绑定；实际动作占用或未完成写入仍会拒绝修改，需按中文原因处理。
换口后刷新列表，按完整 USB 标识选择并保存，再启动通道；在调试台设置机械臂与通道对应并完成人工校准。
原页面素材保留，校准完成时显示是否已沿用；不会凭相同名称、标识后缀或 USB 序号推断旧校准仍有效。
离线回归：`python3 Tests/check_binding_errors.py`（不访问相机或生产服务）。
