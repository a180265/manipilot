# automatic-gesture-profile.v1 — 自动手势/导航只读资格

版本：`automatic-gesture-profile.v1`（规范第 12.3/22.12/22.16 节）。

- `GET /v1/devices/{device_id}/automatic-gestures`：
  `contract_version / device_id / binding(无当前校准时 null) / actions / blocking_reasons`。
- 每个动作条目：`gesture_id / version / state / state_display / calculated /
  controller_supported / contact_validated / effect_validated / usable / blocking_reasons`，
  `state ∈ {unverified,valid,invalid,unsupported}`；return_top 条目另必填
  `top_proof_available`（schema if/then 强制）。
- `usable` 不是"算出来"，必须等于依赖已齐、当前绑定一致且该动作验收有效。
- `GET /v1/devices/{device_id}/automatic-navigation/{profile_id}`：上述元数据 +
  `profile_id / version / owner_plugin / layout_version / navigation_limits`。
- `navigation_limits` 固定 `max_steps=8 / max_physical_actions=8 /
  timeout_ms=90000 / max_boundary_probes=2`。
- WX 只拿引用，不拿 ROI 或机械坐标。

## NAV-03：实时单动作资格

`state=valid` 仅是历史证书状态，不是执行授权。导航与单动作查询使用同一份档案、
开关和当前校准快照计算资格；`usable=true` 必须同时满足：

- 自动手势开关开启；导航还要求自动导航开关开启。
- 设备、daemon 会话、摄像头编号及唯一标识、绑定修订、校准摘要、画面宽高全部一致，
  坐标桥自身哈希与当前校准匹配，证书记录的坐标桥哈希一致。
- 当前规则版本与证书 `recipe_version`、依赖规则版本一致；校准/坐标桥依赖也一致。
- 对应动作 `state=valid` 且 `validation_status=three_passes`。查询保留原验收状态，
  `not_tested` 不得被写成通过，也不得显示触点/效果已验证。
- 动作程序存在，通过 `gesture-program.v1` 校验且与证书 `program_hash` 一致；
  动作编号、条目版本与已发布版本一致。缺程序/摘要的旧证书不能靠查询自动升级。
- 导航档案契约、绑定、容器版本和所选动作的依赖版本匹配；该动作没有阻断原因。

无证书、未验收、完整绑定变化、规则变化、程序缺失/改变及依赖版本变化分别显示明确
中文原因。阻断原因只作用于对应依赖；左侧返回失效时可以选择已验收的右侧返回，
不能用普通列表滑动替代返回或 Home。一个动作可用不代表其他动作可用。

既有响应字段 `validation_status` 在 Schema 中补录为可选字段，保持 v1 兼容；
不改变旧 `screen-gesture.v1` 或旧人工导航接口。

## 同步解析接口（Fleet 内部）

```python
resolve_navigation_action(
    device_id, profile_ref, binding, action, *, bundle, frame, data_root=None
) -> dict
```

`profile_ref` 是请求的 `navigation_profile`，有且仅有
`profile_id/version/contract_version`，契约固定为 `automatic-gesture-profile.v1`。
`binding` 必须精确等于当前查询的完整绑定，包含原八个校准字段以及
`coordinate_frame_hash/gesture_profile_version/recipe_version`；不能省略字段，
不能以布尔值代替版本整数，不能使用发布前的旧容器版本。

成功返回有且仅有 `{gesture_id, program, binding, view}`。
`view` 是完整 `navigation_view` 响应，`binding` 是当前完整绑定的副本；
`program` 是所选动作已验证程序的深拷贝，不是新生成的列表轨迹。

| 请求动作 | 所选证书 |
| --- | --- |
| `search_next` | `scroll_up` |
| `search_previous`、`return_top` | `scroll_down` |
| `back_to_list` | 可用的 `system_back_left`，否则可用的 `system_back_right` |
| `home` | `system_home` |

不合格统一抛 `runtime.errors.ApiError("device_not_eligible", 中文原因)`，对应 HTTP 409。
项目现有异常属性为 `key/detail`，不是新增 `code/message` 构造参数。
提交入口与真正物理动作前都须调用；返回快照不能缓存为持续授权。
调用方提供当前 `bundle/frame`，仍负责实时设备状态、租约、急停、控制器特性及执行开关。
系统动作直接使用返回程序；列表动作的 ROI 适配由执行模块基于已验证参数生成，
不得任意修改速度、保持时间或替换未验收参数。程序不通过公开只读 HTTP 接口暴露给 WX。

三个查询/解析入口均不创建文件、不更新版本、不修复旧证书、不注册模板、不采帧或移动硬件。
档案与程序来自一次内存快照，避免资格和返回程序读取到不同次发布结果。

## 发布与模板

`publish_run_results` 每次原子发布递增容器版本，并同步内置导航档案的版本、完整绑定、
上下滑 `directions` 及五种依赖动作的 `dependency_versions`；未重测动作保留原条目版本，
不会因为另一动作发布而自动变为当前校准下已验收。旧请求必须重新读取引用。
发布接受同一动作结果内的 `program`、兼容 `validated_program` 或 `plan.program`，
规范化为程序和摘要；兼容来源仍须保留真实 `validation_status`。
若上游自检只发布状态而未保存对应验证程序，下游必须明确阻断，不能按默认值重建程序。

正式自检器现在把连续通过的同一滑动程序及 `program_hash` 写入 run，再由发布入口复制
到证书。试验中程序变化会重置连续通过次数，不能把不同程序的成功次数合并。
双击复用原有原子双击能力，不伪造连续滑动程序：证书保存
`validated_primitive={kind:calibrated_double_tap,point,binding,interval_ms:50}`
及 `primitive_hash`，校验实际参数、完整绑定和摘要。该例外仅适用于双击，
不能用于任何列表或系统导航动作，公开响应仍不暴露动作参数。

`return_top` 还须有完整登记且当前绑定匹配的 `top` 模板；
`top_proof_available` 不能仅凭 `kind/confirmed_at` 判真。缺失、损坏、证据结构不完整、
设备/布局或完整绑定不符均使其为 false，且本解析器拒绝该动作。
`home` 同样要求有效 `launcher` 模板，失败只阻断 Home，不阻断列表搜索或返回。
模板遵守 `navigation-template.v1` 的登记记录、明确确认和锚点证据要求，
并校验当前绑定；模板内没有记录的可选容器版本不会因无关动作发布而被伪造补齐。
这只证明模板元数据具备资格，不证明当前画面已匹配；执行模块仍须用新帧确认效果。
本接口没有无模板受限探测的例外授权，也不回退到旧坐标动作。
