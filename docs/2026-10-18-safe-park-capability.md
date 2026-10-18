# 2026-10-18 校准安全停车收尾修复（已双向）

新增 `arm.park`，直接使用当前校准会话确认的 safePark，无页面目标依赖，无 TAP/TAP2。
真实驱动保留校准、实时画面、会话锚点、运动租约、停止/急停、分段及终点核验；
增加安全停车位会话与有限 XYZ 校验。运行时仅豁免输入开关，保留物理/任务/租约门禁。

WX 示例插件升级 1.10.5；保留旧 `park_ime_delete_key` 步骤 ID 保证历史日志兼容，
实际调用改为 arm.park，回执必须明确 arrived、calibration_safe_park、未点击。
发送已确认而收尾失败仍保留发送事实，不重发。executor 与业务事件按新停车角色留证。
未更改已安装冻结版本、运行数据或旧 ui_targets 档案。

旧 ui_targets 的 entries=[] / 外层误包不再影响该纯停车动作；解析器继续严格，
不会解包迁移，也不声称旧目标已重绑。其他 UI 点击继续使用原本合法目标和布局门禁。
arm-01 配置没有显式能力清单，正常 Fleet reload 的 register_device 会用更新后的默认能力
覆盖旧持久 capabilities，新增回归已验证；显式能力清单仍须部署方确认增加。

验证：
- WX：tools/fleet_test.sh tests/test_safe_park_capability.py tests/test_wx_safe_park.py
  tests/test_stage_b_park.py tests/test_wx_manual_sessions.py tests/test_wx_search_navigation.py
  tests/test_gesture_plugins.py -q -p no:cacheprovider → 117 passed，43.04 s。
- SDK：隔离 CAMERA_* 环境，pytest tests/test_safe_park_capability.py
  tests/test_layout_target_driver.py tests/test_platform_isolation.py -q -p no:cacheprovider
  → 50 passed，6.43 s。
- WX 业务：隔离 CAMERA_* 环境，pytest sales_bot/tests/test_outbox_fleet_e2e.py
  -q -p no:cacheprovider → 7 passed，2.92 s。
- 双仓 git diff --check 通过。未做真机测试，由主会话统一部署验收。
- 双仓同步检查：本次停车共享变更无漂移；当时剩余四项由其他进行中的相机/校准修复负责。

已双向：平台能力、驱动、安全门、收尾角色与契约。仅 WX：业务插件、相关业务测试与事件。
源码冻结摘要另见同名 .sha256.json；没有操作硬件、服务、网络 API 或 Git index。
