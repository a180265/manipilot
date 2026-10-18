"""能力注册。

平台只内置与硬件无关的基础能力（移动、停止、回零、点击、截图、输入、
实时画面查看）。任何应用操作能力必须由外部插件注册。
每个能力登记：编号、中文名称、版本、设备类型、输入输出 schema、风险等级、
数据范围、可中断点、清理动作和测试夹具。
"""
from __future__ import annotations

# 平台基础能力（与具体应用无关）
BASE_CAPABILITIES: list[dict] = [
    {
        "capability_id": "arm.wake_screen",
        "name_display": "人工双击唤醒屏幕",
        "version": "1.0.0", "device_type": "arm", "risk_level": "高",
        "data_scope": "校准验证点；不解锁、不读取业务数据、不发送",
        "interruptible": True, "cleanup_action": "不追加动作，释放原租约",
        "test_fixture": "fake-screen-wake", "source": "platform",
        "input_schema": {
            "type": "object", "additionalProperties": False,
            "required": ["binding", "confirmed"],
            "properties": {"binding": {"type": "object"},
                           "confirmed": {"type": "boolean", "const": True}},
        },
        "output_schema": {
            "type": "object",
            "required": ["controller_completed", "tap_count", "effect_status"],
            "properties": {
                "controller_completed": {"type": "boolean"},
                "tap_count": {"type": "integer", "minimum": 0, "maximum": 2},
                "effect_status": {"enum": ["awake", "unverified"]},
            },
        },
    },
    {
        "capability_id": "camera.observe_regions",
        "name_display": "读取已登记区域视觉证据",
        "version": "1.1.0", "device_type": "camera", "risk_level": "低",
        "data_scope": "当前设备页面区域文字、色块、帧号和截图证据；不解释业务结果",
        "interruptible": False, "cleanup_action": "无需清理",
        "test_fixture": "fake-camera", "source": "platform",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "properties": {"layout_state": {"type": "string", "minLength": 1, "maxLength": 64}}},
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "device.gesture_profile",
        "name_display": "读取已校准手势档案",
        "version": "1.0.0", "device_type": "device", "risk_level": "低",
        "data_scope": "本设备已启用手势档案", "interruptible": False,
        "cleanup_action": "无需清理", "test_fixture": "fake-arm", "source": "platform",
        "input_schema": {
            "type": "object", "required": ["profile_id", "version"],
            "properties": {"profile_id": {"type": "string"},
                           "version": {"type": "integer", "minimum": 1}},
            "additionalProperties": False,
        },
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "arm.home",
        "name_display": "机械臂回零",
        "version": "1.0.0",
        "device_type": "arm",
        "risk_level": "高",
        "data_scope": "无业务数据",
        "interruptible": True,
        "cleanup_action": "停止运动",
        "test_fixture": "fake-arm",
        "source": "platform",
        "input_schema": {"type": "object", "additionalProperties": False},
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "arm.park",
        "name_display": "机械臂回到已确认安全停车位",
        "version": "1.0.0", "device_type": "arm", "risk_level": "高",
        "data_scope": "本设备校准安全停车位", "interruptible": True,
        "cleanup_action": "停止运动", "test_fixture": "fake-arm", "source": "platform",
        "input_schema": {"type": "object", "additionalProperties": False},
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "arm.move_preset",
        "name_display": "机械臂移动到预设位置",
        "version": "1.0.0",
        "device_type": "arm",
        "risk_level": "高",
        "data_scope": "无业务数据",
        "interruptible": True,
        "cleanup_action": "回零",
        "test_fixture": "fake-arm",
        "source": "platform",
        "input_schema": {
            "type": "object",
            "required": ["position_id"],
            "properties": {"position_id": {"type": "string"}},
        },
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "arm.move_target",
        "name_display": "机械臂移动到目标位置",
        "version": "1.0.0",
        "device_type": "arm",
        "risk_level": "高",
        "data_scope": "无业务数据",
        "interruptible": True,
        "cleanup_action": "停止运动",
        "test_fixture": "fake-arm",
        "source": "platform",
        "input_schema": {
            "type": "object",
            "required": ["target_id"],
            "properties": {"target_id": {"type": "string"}},
        },
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "arm.click",
        "name_display": "机械臂点击",
        "version": "1.2.0",
        "device_type": "arm",
        "risk_level": "高",
        "data_scope": "无业务数据",
        "interruptible": True,
        "cleanup_action": "回零",
        "test_fixture": "fake-arm",
        "source": "platform",
        "input_schema": {
            "type": "object",
            "required": ["target_id"],
            "properties": {
                "target_id": {"type": "string"},
                "tapDwellMs": {
                    "type": "number", "minimum": 0, "maximum": 5000,
                    "description": "本次点击的额外接触停留毫秒，覆盖布局和校准默认值；"
                    "0 表示不额外等待，仍包含下压和抬起时间。越界或非有限数在运动前拒绝。",
                },
                "click_attempt": {
                    "type": "integer", "enum": [0, 1, 2], "default": 0,
                    "description": "0 默认点原中心，本台启用避边策略时在安全区选点；"
                    "1/2 请求同目标安全区内的替代落点。"
                    "仅会话行、返回键和输入框支持，每次调用只点击一次；"
                    "调用方必须先确认上次未生效，未知结果禁止重试。",
                },
            },
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "click_attempt": {"type": "integer", "enum": [0, 1, 2]},
                "click_retry_supported": {"type": "boolean"},
                "click_point": {"type": "object", "description": "本次屏幕/相机落点及安全区来源，不含识别文本"},
            },
        },
    },
    {
        "capability_id": "arm.double_tap",
        "name_display": "机械臂双击",
        "version": "1.1.0",
        "device_type": "arm",
        "risk_level": "高",
        "data_scope": "无业务数据",
        "interruptible": True,
        "cleanup_action": "停止运动",
        "test_fixture": "fake-arm",
        "source": "platform",
        "input_schema": {
            "type": "object",
            "anyOf": [{"required": ["target_id"]}, {"required": ["point", "binding"]}],
            "properties": {
                "target_id": {"type": "string"},
                "target_ref": {"type": "object"},
                "point": {"type": "object"},
                "binding": {"type": "object"},
                "intervalMs": {"type": "number", "minimum": 50, "maximum": 500},
            },
        },
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "arm.swipe",
        "name_display": "机械臂滑动",
        "version": "1.1.0",
        "device_type": "arm",
        "risk_level": "高",
        "data_scope": "无业务数据",
        "interruptible": True,
        "cleanup_action": "停止运动",
        "test_fixture": "fake-arm",
        "source": "platform",
        "input_schema": {
            "type": "object",
            "required": ["start", "end"],
            "properties": {
                "start": {"type": "object"},
                "end": {"type": "object"},
                "binding": {"type": "object"},
                "durationMs": {"type": "number", "minimum": 150, "maximum": 5000},
                "feed": {"type": "number", "maximum": 3000},
                "tapDepth": {"type": "number"},
                "tapFeed": {"type": "number"},
            },
        },
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "arm.stop",
        "name_display": "机械臂停止",
        "version": "1.0.0",
        "device_type": "arm",
        "risk_level": "低",
        "data_scope": "无业务数据",
        "interruptible": False,
        "cleanup_action": "无需清理",
        "test_fixture": "fake-arm",
        "source": "platform",
        "input_schema": {"type": "object", "additionalProperties": False},
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "camera.capture",
        "name_display": "摄像头截图取证",
        "version": "1.0.0",
        "device_type": "camera",
        "risk_level": "低",
        "data_scope": "截图与哈希（受控证据引用）",
        "interruptible": False,
        "cleanup_action": "无需清理",
        "test_fixture": "fake-camera",
        "source": "platform",
        "input_schema": {"type": "object", "additionalProperties": False},
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "camera.confirm_view",
        "name_display": "摄像头画面确认",
        "version": "1.0.0",
        "device_type": "camera",
        "risk_level": "低",
        "data_scope": "界面状态观测",
        "interruptible": False,
        "cleanup_action": "无需清理",
        "test_fixture": "fake-camera",
        "source": "platform",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "input.type_text",
        "name_display": "文本原样输入",
        "version": "1.0.0",
        "device_type": "input",
        "risk_level": "中",
        "data_scope": "任务给定文本（不得修改）",
        "interruptible": True,
        "cleanup_action": "清空输入缓存",
        "test_fixture": "fake-input",
        "source": "platform",
        "input_schema": {
            "type": "object",
            "required": ["text"],
            "properties": {"text": {"type": "string"}},
        },
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "camera.identify_ui_state",
        "name_display": "识别界面状态",
        "version": "1.0.0",
        "device_type": "camera",
        "risk_level": "低",
        "data_scope": "界面状态观测（内部键+中文+置信度）",
        "interruptible": False,
        "cleanup_action": "无需清理",
        "test_fixture": "fake-camera",
        "source": "platform",
        "input_schema": {"type": "object", "additionalProperties": False},
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "camera.identify_account_conversation",
        "name_display": "识别账号与会话线索",
        "version": "1.0.0",
        "device_type": "camera",
        "risk_level": "低",
        "data_scope": "账号/会话线索观测与置信度",
        "interruptible": False,
        "cleanup_action": "无需清理",
        "test_fixture": "fake-camera",
        "source": "platform",
        "input_schema": {"type": "object", "additionalProperties": False},
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "camera.locate_text",
        "name_display": "摄像头 OCR 定位文字",
        "version": "1.1.0",
        "device_type": "camera",
        "risk_level": "低",
        "data_scope": "界面文字及摄像头像素框",
        "interruptible": False,
        "cleanup_action": "无需清理",
        "test_fixture": "fake-camera",
        "source": "platform",
        "input_schema": {
            "type": "object", "required": ["text"],
            "properties": {
                "text": {"type": "string"}, "region": {"type": "string"},
                "min_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "exact": {"type": "boolean", "default": False},
                "localization_mode": {"type": "string", "enum": ["layout", "screen_text"],
                                      "default": "layout"},
                "previous_location_id": {"type": "string", "minLength": 1},
                "roi": {
                    "type": "object",
                    "required": ["x1", "y1", "x2", "y2", "frameW", "frameH"],
                    "properties": {
                        "x1": {"type": "number", "minimum": 0},
                        "y1": {"type": "number", "minimum": 0},
                        "x2": {"type": "number", "minimum": 0},
                        "y2": {"type": "number", "minimum": 0},
                        "frameW": {"type": "number", "exclusiveMinimum": 0},
                        "frameH": {"type": "number", "exclusiveMinimum": 0},
                    },
                    "additionalProperties": False,
                },
            },
            "allOf": [{
                "if": {"properties": {"localization_mode": {"const": "screen_text"}},
                       "required": ["localization_mode"]},
                "then": {"properties": {"exact": {"const": True}}, "required": ["exact"]},
            }, {
                "if": {"required": ["previous_location_id"]},
                "then": {"properties": {"exact": {"const": True},
                                        "localization_mode": {"const": "screen_text"}},
                         "required": ["exact", "localization_mode"]},
            }],
        },
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "camera.live_view",
        "name_display": "实时画面只读查看",
        "version": "1.0.0",
        "device_type": "camera",
        "risk_level": "低",
        "data_scope": "只读画面流，不录制、不落业务事件",
        "interruptible": True,
        "cleanup_action": "关闭查看会话",
        "test_fixture": "fake-camera",
        "source": "platform",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
    },
    # 自动手势闭环（规范第 12.4 节）
    {
        "capability_id": "device.automatic_gesture_profile",
        "name_display": "读取自动手势档案",
        "version": "1.0.0", "device_type": "device", "risk_level": "低",
        "data_scope": "本设备自动手势资格与证书引用；不暴露机械坐标",
        "interruptible": False, "cleanup_action": "无需清理",
        "test_fixture": "fake-device", "source": "platform",
        "input_schema": {"type": "object", "additionalProperties": True},
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "device.gesture_capture",
        "name_display": "手机手势采样协调（仅自检）",
        "version": "1.0.0", "device_type": "device", "risk_level": "中",
        "data_scope": "测试页准备、复位与轨迹证据收集；不直接产生机械动作",
        "interruptible": False, "cleanup_action": "结束采样会话",
        "test_fixture": "fake-device", "source": "platform",
        "input_schema": {"type": "object", "additionalProperties": True},
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "arm.execute_gesture_program",
        "name_display": "执行手势程序（连续路径）",
        "version": "1.0.0", "device_type": "arm", "risk_level": "高",
        "data_scope": "无业务数据", "interruptible": True,
        "cleanup_action": "停止运动", "test_fixture": "fake-arm",
        "source": "platform",
        "input_schema": {"type": "object", "additionalProperties": True},
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "input.ime_create",
        "name_display": "受控输入法候选创建",
        "version": "1.0.0", "device_type": "input", "risk_level": "高",
        "data_scope": "按任务许可创建输入法候选；不接受调用方指定手机或编号",
        "interruptible": False, "cleanup_action": "取消候选（经控制协议）",
        "test_fixture": "fake-input", "source": "platform",
        "input_schema": {"type": "object", "additionalProperties": True},
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "camera.measure_scroll",
        "name_display": "通用列表位移测量",
        "version": "1.0.0", "device_type": "camera", "risk_level": "低",
        "data_scope": "画面位移测量与证据；不生成客户身份",
        "interruptible": False, "cleanup_action": "无需清理",
        "test_fixture": "fake-camera", "source": "platform",
        "input_schema": {"type": "object", "additionalProperties": True},
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "camera.measure_probe_path",
        "name_display": "探头实体路径证据",
        "version": "1.0.0", "device_type": "camera", "risk_level": "低",
        "data_scope": "探头位移测量证据；证明实体运动不证明屏幕接触",
        "interruptible": False, "cleanup_action": "无需清理",
        "test_fixture": "fake-camera", "source": "platform",
        "input_schema": {"type": "object", "additionalProperties": True},
        "output_schema": {"type": "object"},
    },
    {
        "capability_id": "arm.test_calibrated_double_tap",
        "name_display": "校准双击自检（仅自检试验）",
        "version": "1.0.0", "device_type": "arm", "risk_level": "高",
        "data_scope": "无业务数据", "interruptible": True,
        "cleanup_action": "停止运动", "test_fixture": "fake-arm",
        "source": "platform",
        "input_schema": {"type": "object", "additionalProperties": True},
        "output_schema": {"type": "object"},
    },
]


# 设备默认能力清单（仿真设备全量支持基础能力）
DEFAULT_DEVICE_CAPABILITIES: list[str] = [
    "arm.wake_screen",
    "camera.observe_regions",
    "device.gesture_profile",
    "arm.home",
    "arm.park",
    "arm.move_preset",
    "arm.move_target",
    "arm.click",
    "arm.double_tap",
    "arm.swipe",
    "arm.stop",
    "camera.capture",
    "camera.confirm_view",
    "camera.identify_ui_state",
    "camera.identify_account_conversation",
    "camera.locate_text",
    "input.type_text",
    "input.ime_create",
    "camera.live_view",
    "device.automatic_gesture_profile",
    "device.gesture_capture",
    "arm.execute_gesture_program",
    "camera.measure_scroll",
    "camera.measure_probe_path",
    "arm.test_calibrated_double_tap",
]


class CapabilityRegistry:
    def __init__(self, store):
        self._store = store

    def bootstrap(self) -> None:
        """注册平台基础能力（幂等）。"""
        for capability in BASE_CAPABILITIES:
            existing = self._store.get_capability(capability["capability_id"])
            platform_upgrade = (
                existing is not None and existing.get("source") == "platform"
                and capability["capability_id"] in {
                    "arm.click", "arm.double_tap", "arm.swipe", "camera.locate_text",
                    "camera.observe_regions"}
                and existing.get("version") == "1.0.0" and capability["version"] == "1.1.0")
            if (existing is not None and existing.get("source") == "platform"
                    and capability["capability_id"] == "arm.click"
                    and existing.get("version") in {"1.0.0", "1.1.0"}
                    and capability["version"] == "1.2.0"):
                platform_upgrade = True
            if existing is None or platform_upgrade:
                self._store.save_capability(dict(capability))

    def register(self, capability: dict) -> dict:
        """插件注册能力：必须带齐中文名称、含义、风险、失败处理等登记项。"""
        required = [
            "capability_id",
            "name_display",
            "version",
            "device_type",
            "risk_level",
            "data_scope",
            "cleanup_action",
            "source",
        ]
        missing = [field for field in required if not capability.get(field)]
        if missing:
            from runtime.errors import ApiError

            raise ApiError("plugin_manifest_invalid", f"能力登记缺少字段：{missing}")
        self._store.save_capability(dict(capability))
        return dict(capability)

    def get(self, capability_id: str) -> dict | None:
        return self._store.get_capability(capability_id)

    def all(self) -> list[dict]:
        return sorted(self._store.all_capabilities(), key=lambda c: c["capability_id"])

    def platform_capability_ids(self) -> set[str]:
        return {c["capability_id"] for c in self.all() if c.get("source") == "platform"}
