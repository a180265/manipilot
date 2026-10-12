# 已保存屏幕手势契约

版本：`saved-screen-gesture.v1`，日期：2026-09-14。
独立于旧 `gesture-profile.v1` 导航档案；不修改 `screen-gesture.v1` 任务或插件。
保存只登记计算后的屏幕点，不执行动作，不取得运动租约，不证明手机效果。

## 公共只读接口

- `GET /v1/devices/{device_id}/saved-screen-gestures`：
  `{contract_version: "saved-screen-gesture.v1", device_id, gestures: [entry]}`
- `GET /v1/devices/{device_id}/saved-screen-gestures/{gesture_id}`：返回 `entry`。

公共接口不提供保存、删除或执行操作。设备不存在返回既有 `device_not_found`。
尚无档案时列表为空；指定条目不存在返回 HTTP 404 `saved_gesture_not_found`。
非法编号返回 HTTP 400 `saved_gesture_invalid`；
档案损坏、设备身份不符或路径为符号链接返回 HTTP 409 `saved_gesture_archive_invalid`，
绝不回退旧档案、返回可用的空白条目或覆盖损坏文件。
错误沿用 `{error: {key, title, suggestion, trace_id, detail}}` 结构。

## 条目

`entry` 包含 `contract_version`、`device_id`、`gesture_id`、`version`、`label`、
`action`、`binding`、`payload`、`usable`、`blocking_reasons`、
`effect_status`、`effect_status_display`、`created_by`、`created_at`、
`updated_by`、`updated_at`。

- 编号为 1~64 位字母数字、下划线或连字符，首位须为字母或数字，按设备隔离。
- `label` 是原样保存的非空业务名称，最多 100 字；可为“Home”“Back”，
  Fleet 不解释名称，不据此选择动作或断言返回桌面/返回上一页成功。
- `version` 从 1 开始，更新递增；创建人和创建时间保留，更新人和时间替换。
  时间为带时区 ISO 8601。
- `binding` 是完整 `screen_gestures.binding_snapshot`，与 `payload.binding` 完全相同，
  包含设备、控制器会话、物理摄像头、绑定版本、完整 H/XY/Z/屏幕/停车位摘要及帧尺寸。
- `payload` 恰好为既有 `screen-gesture.v1` 的 payload：
  双击为 `{action:"double_tap",binding,point:{x,y},interval_ms}`；
  滑动为 `{action:"swipe",binding,start:{x,y},end:{x,y},duration_ms}`。
  不包含档案指针、相机坐标或机械参数。
- `usable` **仅表示保存的绑定、几何、时长与当前校准及已知控制器会话一致**。
  公共 GET 使用运行时已知会话，不连接硬件、不恢复设备；会话未知时返回不可用。
  不含特性、实体核验、急停、租约、外部页面占用等实时执行资格。
  执行前仍须查询 `screen-gestures` 元数据，并经过既有任务和驱动门禁。
- 当前校准变化、失效、会话不符或点位无法安全计算时仍返回原条目，
  `usable:false` 并附中文原因；GET 不改点、不改时长、不重绑、不写档案。
- `effect_status` 永远为 `unverified`，显示固定为“手机效果未核验”。

## 本机保存调用

```python
list_gestures(device, bundle, session, data_root=None) -> list[dict]
get_gesture(device, gesture_id, bundle, session, data_root=None) -> dict
save_gesture(device, body, bundle, data_root=None) -> dict
```

`drivers.saved_screen_gestures` 导出以上函数。
`bundle` 为调用者核对过的当前校准，`session` 为当前控制器会话或 `None`；
本机保存入口负责既有设备上下文、操作者与权限校验。保存本身不检查实时运动资格。
错误 `SavedGestureError(ValueError)` 带 `.code`、`.http_status` 及中文 `str(error)`。

`body` 严格包含 `label`、`operator`、`action`、`point` 或 `start/end`，
双击必须有 `interval_ms`，可选 `gesture_id`、`expected_version`。
点仅为归一化 `{x,y}`；调试台把相机标记经现有服务器反解后传入。
双击间隔为 50~500 毫秒的整数、步长 10；滑动时长只能由 `plan_gesture` 重新计算，
禁止调用者提交 `duration_ms`、Z 参数、binding、确认成功等额外字段。

省略 `gesture_id` 时产生 `gesture-` 加随机编号；也可指定尚不存在的合法编号创建，
创建不带 `expected_version`。更新必须指定已存在编号和当前正整数版本；
缺版本或版本冲突返回 409 `saved_gesture_version_conflict`。
编号不存在却带版本返回 404，不自动创建。
更新要求原绑定与当前完整绑定相同；否则 409 `saved_gesture_binding_changed`，
必须重新标记并另存，不允许标签更新悄然重绑旧动作。
校准不可计算时返回 409 `saved_gesture_calibration_invalid`。

档案位置：`data/devices/{device_id}/saved_screen_gestures.json`。
文件锁覆盖读取、版本比较与写入；写入使用同目录临时文件、文件同步、
原子替换及目录同步。档案独立于旧 `gesture_profiles.json`。
保存不会修改校准、机械参数或动作结果。

## WX 使用

WX 选择一个可用条目后，将该版本的完整 `entry.payload` 复制为不可变任务载荷，
与当前屏幕手势元数据再次核对，提交既有 `task_type:"screen_gesture"`、
`contract_version:"screen-gesture.v1"` 任务。档案后续更新不能改变已经物化的任务。
不新增插件能力、执行指针或任务契约字段；未知动作不重放，手机效果由业务独立核验。
