# 校准手势档案契约 v1

日期：2026-09-11。向 `wx-session.v1`、`chat-send.v1` 增加可选引用字段，
不改变未配置导航的旧任务行为。需要 Arm Fleet 新版能力及 WX 插件 1.4.1。

## 读取接口

`GET /v1/devices/{device_id}/gesture-profiles/{profile_id}`，只读，不获取运动租约。
WX 使用约定编号 `wx_chat_list`；其他应用可登记自己的编号和所属插件。

```json
{
  "contract_version": "gesture-profile.v1",
  "device_id": "arm-01",
  "profile_id": "wx_chat_list",
  "version": 1,
  "owner_plugin": "wx-wechat-adapter",
  "state": "enabled",
  "usable": true,
  "blocking_reasons": []
}
```

| 字段 | 中文含义及规则 |
|---|---|
| profile_id | 档案编号，1~64 位字母、数字、下划线或连字符 |
| version | 坐标/参数版本，正整数；每次保存草稿递增 |
| owner_plugin | 获准解析此档案的插件，不允许跨插件调用 |
| state | draft 草稿、testing 测试中、tested 待确认、enabled 已启用、disabled 已停用 |
| usable | 仅表示档案证据、启用状态、绑定和版本可解析，不代表设备在线或完整业务可执行 |
| blocking_reasons | 档案不可用的中文原因列表 |

接口不返回坐标、校准文件路径或参考图。档案缺失返回 404；
旧服务没有该接口也可能返回 404，WX 兼容为不启用导航，不能显示为导航已就绪。
其他网络错误、服务错误、字段不符、已存在但不可用的档案都在动作提交前拦截。
版本查询不会延长任务有效期，也不证明物理动作成功。

## 任务引用

```json
{"navigation_profile": {"profile_id": "wx_chat_list", "version": 1}}
```

仅作为打开聊天或未持有会话的发送任务 `payload` 可选字段。
不可与旧 `navigation` 同传。持有会话后发送、退出继承打开时引用，
不切换到较新坐标版本。修改/停用旧档案后，需先人工处理原会话。

`device.gesture_profile` 中文名“读取已校准手势档案”，是低风险只读能力，
插件必须在清单声明，设备必须允许该能力，运行时按任务设备及调用插件解析。
参数为 profile_id/version，返回 config；校准、相机会话、设备、画面尺寸或高度
变化返回 `driver_uncalibrated`，未授权返回能力拒绝，不允许坐标回退。
每次滑动前重新解析引用，运动仍由 `arm.swipe` 逐次验证绑定、租约与安全范围。
停止、急停、取消或动作结果未知后禁止继续滑动，不自动重放任务。

调试台写接口属于 Arm Fleet 内部调试上下文，不是 WX 接口：
保存草稿、一次方向测试、人工确认启用、停用均要求当前设备上下文和操作人；
测试还需真实动作二次确认。测试证据由服务端产生，客户端不能上传成功证明。
改动原有 OCR/输入法校准不会被手势档案覆盖，而会使手势档案失效待复测。

2026-09-14 兼容加固：方向测试只有明确 `swipe_executed=True`、
`ok=True`、`motion_verified=True` 且无中止/未知/矛盾状态时才能认证完成。
读取/启用已有档案也复核保存的完成标志，不把旧 ACK-only 证据当作通过。
真实微信滑动与通用调试采用同一机械完成判据；页面、顶部区域及业务身份另验。
