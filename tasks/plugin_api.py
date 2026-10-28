"""任务插件 SDK：外部插件必须实现的统一生命周期接口。

平台只校验插件声明、分配资源、执行生命周期并接收结构化结果；
具体应用流程（微信、拍照、刷视频等）全部由插件实现。

接口（语义遵循 docs/通用硬件任务运行时规划.md 第七节）：

    validate(task, device_context) -> 参数和权限结果
    validate_preconditions(task, device_context, *, data_root, evidence_root, evidence_get) -> 同步实时资格
    plan(task, device_context) -> 不含坐标细节的步骤计划
    execute_step(step, runtime_context) -> 步骤结果和证据
    recover(failure, runtime_context) -> 恢复动作和安全结果
    cleanup(runtime_context) -> 清理和归位结果
    cancel(runtime_context) -> 停止结果

插件不得绕过运行时直接点击：插件代码只能通过 StepContext 提供的
call_capability / capture_evidence 访问硬件，未声明的能力会被拒绝并审计。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
from typing import Any

# 步骤结果允许的 outcome
STEP_OUTCOMES = {"success", "failed", "unknown"}


class PluginViolationError(Exception):
    """插件试图越权（未声明能力/资源）时抛出，运行时会拦截并审计。"""


class StepContext:
    """运行时交给可信插件的受控上下文，不是 Python 安全沙箱。

    正式能力入口校验任务归属、租约、资源和动作预算。进程内插件必须可信，
    本门面不能隔离主动导入模块或访问 Python 私有对象的不可信代码。
    """

    def __init__(self, runtime, task: dict, plugin: dict):
        self._runtime = runtime
        self._task = task
        self._plugin_id = plugin["plugin_id"]
        self._device_id = task["device_id"]
        self._limits = (plugin.get("limits") or {}).get("max_actions_per_task")

    @property
    def task(self) -> dict:
        """任务只读快照；插件修改副本不会改写平台任务、设备或正文。"""
        return deepcopy(self._task)

    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def data_root(self):
        """本设备运行根；不由插件安装目录推断，也不修改模块全局变量。"""
        return self._runtime._gesture_data_root(self.device_id)

    @property
    def evidence_root(self):
        return self._runtime.config.evidence_dir

    def get_evidence(self, evidence_id: str) -> dict | None:
        """本设备证据元数据只读快照；不采帧，也不暴露其他设备记录。"""
        record = self._runtime.evidence.get(evidence_id)
        if record is None or record.get("device_id") != self.device_id:
            return None
        return deepcopy(record)

    @property
    def device_context(self) -> dict:
        """设备只读上下文：绑定账号、能力、界面状态等。"""
        device = self._runtime.devices.get(self.device_id)
        return {
            "device_id": device["device_id"],
            "wechat_account_id": device.get("wechat_account_id"),
            "camera_id": device.get("camera_id"),
            "capabilities": list(device.get("capabilities", [])),
            "current_ui_state": device.get("current_ui_state"),
            "current_ui_state_display": device.get("current_ui_state_display"),
            "binding_mode": device.get("binding_mode"),
            "daemon_session": device.get("daemon_session"),
            "ime_device_id": self._runtime.devices.input_binding(self.device_id).get("ime_device_id"),
        }

    async def call_capability(self, capability_id: str, params: dict | None = None) -> dict:
        """调用已声明的基础能力（唯一合法的硬件访问途径）。"""
        return await self._runtime.plugin_invoke_capability(
            self.plugin_id, self._task, capability_id, params or {}
        )

    async def capture_evidence(self, label: str) -> dict:
        """截图取证（平台强制要求所有插件留证）。"""
        return await self._runtime.plugin_capture_evidence(self.plugin_id, self._task, label)

    async def update_observed_ui_state(self, ui_state: str, confidence: float) -> None:
        """把观测到的界面状态写回设备镜像（只读事实回报）。"""
        self._runtime.validate_plugin_context(self.plugin_id, self._task)
        self._runtime.devices.update_ui_state(self.device_id, ui_state, confidence)

    # ---- 插件私有持久状态（结果未知/去重指纹等） ----
    def state_get(self, key: str) -> Any:
        return self._runtime.plugin_state_get(self.plugin_id, self.device_id, key)

    def state_set(self, key: str, value: Any) -> None:
        self._runtime.validate_plugin_context(self.plugin_id, self._task)
        self._runtime.plugin_state_set(self.plugin_id, self.device_id, key, value)

    def check_action_budget(self) -> int:
        """剩余可执行动作次数；无上限时返回 -1。"""
        if self._limits is None:
            return -1
        used = int(self._task.get("action_call_count", self._task.get("capability_call_count", 0)))
        return max(self._limits - used, 0)


class ReviewContext:
    """人工核对只允许整理插件私有状态，不授予动作或观测写入能力。"""

    def __init__(self, runtime, task: dict, plugin: dict):
        self._runtime = runtime
        self.task = task
        self.plugin_id = plugin["plugin_id"]
        self.device_id = task["device_id"]
        self._pending: dict[str, Any] = {}

    @property
    def device_context(self) -> dict:
        return dict(self._runtime.devices.get(self.device_id))

    @property
    def data_root(self):
        """本设备台账根；人工核对不得从插件安装路径推断运行数据。"""
        return self._runtime._gesture_data_root(self.device_id)

    def state_get(self, key: str) -> Any:
        if key in self._pending:
            return self._pending[key]
        return self._runtime.plugin_state_get(self.plugin_id, self.device_id, key)

    def state_set(self, key: str, value: Any) -> None:
        self._pending[key] = value

    def commit(self) -> None:
        for key, value in self._pending.items():
            self._runtime.plugin_state_set(self.plugin_id, self.device_id, key, value)


class TaskPlugin(ABC):
    """外部插件基类。plugin_id / manifest 由插件清单提供。"""

    plugin_id: str = ""
    manifest: dict = {}

    def validate_preconditions(self, task: dict, device_context: dict, *, data_root,
                               evidence_root=None, evidence_get=None) -> dict:
        """同步、只读、无运动。提交、出队及业务物理调用前复核；旧插件默认通过。

        返回 {ok, error_category?, message?}，也可抛 ApiError。不得缓存资格；
        平台在异步规划结束、真正调用驱动前再次执行，不拦截停止或安全归位。
        """
        return {"ok": True}

    @abstractmethod
    async def validate(self, task: dict, device_context: dict) -> dict:
        """参数和权限校验。返回 {ok: bool, error_category?, message(中文)}。"""

    @abstractmethod
    async def plan(self, task: dict, device_context: dict) -> list[dict]:
        """步骤计划。步骤必须包含：step_no/action/action_display/is_action/
        required_capabilities/timeout_seconds/success_criteria/evidence_required。
        计划不得包含窗口坐标等实现细节。"""

    @abstractmethod
    async def execute_step(self, step: dict, ctx: StepContext) -> dict:
        """执行步骤。返回 {outcome: success|failed|unknown, error_category?,
        message(中文), detail{}}；detail 可带 provider_message_id 等结果扩展。

        抛异常也受支持：ApiError / RegionContextError 表示"业务级拒绝"，平台按
        是否已发生动作二分——未动作且错误键在白名单内 → failed（可重试），
        否则 → unknown（人工核对）。其余异常一律按 unknown 处理；插件不要为了
        拿到 failed 而把"不确定是否动过"的失败伪装成业务拒绝。"""

    async def recover(self, failure: dict, ctx: StepContext) -> dict:
        """失败恢复动作。默认：停止并请求归位。"""
        try:
            await ctx.call_capability("arm.stop")
        except Exception:
            pass
        return {"recovered": False, "message": "插件默认恢复：已请求停止，等待运行时归位"}

    async def cleanup(self, ctx: StepContext) -> dict:
        """清理现场并回到安全状态。默认：清空输入缓存。"""
        return {"cleaned": True, "message": "插件默认清理完成"}

    async def cancel(self, ctx: StepContext) -> dict:
        """取消：立即停止物理动作。默认：请求停止。"""
        try:
            await ctx.call_capability("arm.stop")
        except Exception:
            pass
        return {"stopped": True, "message": "已请求停止"}

    def review_task(self, task: dict, ctx: ReviewContext, review_context: dict) -> dict:
        """核对后的上下文释放；旧插件默认拒绝，不推测应用状态。"""
        return {"context_released": False, "message": "插件尚未提供人工核对恢复"}


def make_step(
    step_no: int,
    action: str,
    action_display: str,
    *,
    is_action: bool,
    required_capabilities: list[str],
    timeout_seconds: int = 15,
    success_criteria: dict | None = None,
    evidence_required: bool = True,
    cleanup_action: str = "返回安全状态",
    params: dict | None = None,
) -> dict:
    """构造规范步骤。success_criteria 必须结构化（例如界面状态、哈希一致）。"""
    return {
        "step_no": step_no,
        "action": action,
        "action_display": action_display,
        "is_action": is_action,
        "required_capabilities": required_capabilities,
        "timeout_seconds": timeout_seconds,
        "success_criteria": success_criteria or {},
        "evidence_required": evidence_required,
        "cleanup_action": cleanup_action,
        "params": params or {},
    }
