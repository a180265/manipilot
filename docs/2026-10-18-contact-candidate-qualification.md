# 2026-10-18 接触阶段屏幕候选误判修复

本轮仅完成离线源码和回归验证；共享实现已双向移植到 WX arm_fleet 与 manipilot。
没有操作机械臂、服务、串口、运行期数据库或 Git index，不据此宣称现场动作、
完整 WX 发送流程或平台全部必选项验收通过。部署和现场验收由主会话继续。

## 问题与根因

v12 接近触屏后，真实四边仍可严格拟合，相对本次动作的清晰参照最大变化
1.2806248475 相机像素，低于原有 2 px 接触约束。但通用候选检测的支架小反光
或屏内头像触发多屏拒绝，真实 gate 返回 screen_pose_unavailable，发生在接触前。
一方面候选列表最多取两个，后过滤可能隐藏后续暗屏；另一方面把通用轮廓候选
直接等同于物理屏幕，未先利用当前实测屏幕边界区分内部内容。

## 变更和明确边界

- layout_region._screen_candidates 默认仍在两个候选处提前返回，维持非接触路径
  对小型独立第二屏的既有拒绝；新增 limit=None 供接触路径完整枚举亮/暗两极性。
- 接触路径先调用既有严格 locate_screen_quad，要求当前四边可测、可拟合、未裁断，
  使用动作内清晰参照限制实测变化，不使用参照角点替代当前测量，也不走梯度兜底。
- 仅在这次严格定位成功后，以当前实测四边形排除完全处于屏内的矩形内容。
  外部矩形沿用 screen_edge.MIN_AREA_RATIO=0.05，面积至少为粗框裁图 5%
  才在接触分支独立判为有效屏幕；保留既有候选差异 3 px 判断。即使被遮挡的手机
  不在通用列表，另一个满足条件的外部候选也足以拒绝。
- 明确限制：接触分支中小于粗框面积 5% 的外部矩形不再单独作为第二屏。
  不声称能检测任意小外部显示器。非接触路径的小第二屏拒绝保持原义，
  原 test_screen_candidate_shapes.py 未修改。
- 四边、动作参照 2 px、裁断、来源绑定、帧序列、新鲜度、参照过期等规则未放宽。
  未改 screen_pose.py、拟合容差、RANSAC 或 H2。

## 回归证据

新 tests/test_contact_candidate_qualification.py 走实际
check_live_calibration → 区域定位 → contact reference → 接触复检链，
不 mock 几何定位结果。覆盖屏内头像、屏内大矩形、外部小反光、接近造成桥接，
真实外部第二屏、在前两个候选之后的第三暗屏、移动 3 px、缺失侧边、裁断，
并有本机私有 v12 原图的精确 SHA-256 回放；原图不入库。

修复前新文件实测 4 failed / 7 passed：小反光、桥接头像、完整枚举接口和
真实 v12 接触回放失败。修复后 11 passed；后续强化大矩形桥接场景也通过。
原 test_contact_reference_gate.py 的第二屏夹具更新为左侧 105×420 px 真矩形，
同时满足已有短长边比和本次明确的 5% 接触面积边界。

最终定向结果：

- WX 67 passed：新回归、动作参照 gate、接触四边、候选形状、光照定位五个文件。
- SDK 71 passed / 1 skipped：同五文件加平台隔离；私有 v12 经环境变量指向本机
  WX 原图实际通过。skip 是既有另一个私有 capture 载具缺失。
- 相邻区域/摆正一致性两文件，两仓当前均 39 passed / 2 failed；
  仅在临时内存 overlay 逆转本次两函数补丁后，同环境仍各 39 passed / 2 failed。
  旧红为 excessive-movement 的 22 px 判据及 installed_region_missing_pointer；
  本轮不修改其行为或扩大变更。
- 原始诊断脚本不带 prototype 再跑：pre_checks=[]、contact_checks=[]，
  实测角点 [[117.0,69.2],[1154.9,81.4],[1159.8,549.6],[102.4,539.6]]。

WX 命令：

```sh
tools/fleet_test.sh tests/test_contact_candidate_qualification.py tests/test_contact_reference_gate.py tests/test_screen_edge_contact_reference.py tests/test_screen_candidate_shapes.py tests/test_layout_lighting_geometry.py -q
tools/fleet_test.sh tests/test_crop_pose_consistency.py tests/test_pre_enable_region_guards.py -q
.venv-wx/bin/python .local/calibration-final-20261018/diagnose_v12_contact_candidates.py
.venv-wx/bin/python tools/check_fleet_sync.py
```

SDK 使用 WX .venv-wx/bin/pytest，在 SDK cwd 运行同样文件并添加
tests/test_platform_isolation.py，移除 CAMERA_CONTROL_TOKEN_FILE、
CAMERA_HOST_INSTALLATION_ID_FILE、CAMERA_HELPER_EXEC 三个产线环境变量；
SCREEN_EDGE_V12_CAPTURE_DIR 仅指向本机私有证据目录。

原始日志存于 WX .local/calibration-final-20261018/contact-candidates-*.log。
baseline overlay 位于同目录 contact_baseline_overlay.py，只在测试解释器内执行，
不写生产源码。双仓同步检查仍报警四项主会话既有漂移：
adapters/screen_ime/camera_calibration.py、drivers/calibration_auto_runner.py、
drivers/screen_pose.py、drivers/screen_proof.py；本次两生产文件逐字一致。
四项漂移不由本子任务定案或宣称清零。

## 生产文件冻结

- runtime/layout_geometry.py：
  22b74850b31a684bc7a4ebc8f6bc9d76510d9790d22fe1f8f7a3243e49c6d6a6
- runtime/layout_region.py：
  f830f6188be408899b44b99acd58c4ef4f430657fd8bc1c46e6d0907fd44d363

全部本轮交付文件清单与哈希见同目录
2026-10-18-contact-candidate-qualification.sha256.json；
验收总表可能由主会话追加现场记录，应以对应冻结时刻为准。

