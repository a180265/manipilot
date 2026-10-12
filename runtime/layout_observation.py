"""Read-only selected layout resolution and single-frame visual observation."""
from __future__ import annotations

import copy
import hashlib

import cv2
import numpy as np

from drivers import screen_pose, screen_proof
from runtime.layout_geometry import LayoutGeometryError, mapping_error_bound, matrix_and_size
from runtime.layout_matching import (
    apply_target_safety, compute_page_check, PASS_MARGIN, PASS_SCORE, _pixel_rect,
)
from runtime.layout_state import content_sha


STATES = ("wechat_list", "chat_ime_hidden", "chat_ime_empty", "chat_ime_text")
BINDING_KEYS = ("camera_id", "camera_device_unique_id", "camera_binding_id",
                "daemon_binding_id", "arm_binding_id", "ime_device_id",
                "source_geometry_id", "binding_revision")


class LayoutObservationError(ValueError):
    def __init__(self, message, reason_code="archive_invalid"):
        super().__init__(message)
        self.reason_code = reason_code


def _require(condition, message, code="archive_invalid"):
    if not condition:
        raise LayoutObservationError(message, code)


def _validate(record, kind):
    from jsonschema import Draft7Validator
    from runtime.layout_wire import _frozen_registry

    registry = _frozen_registry()
    uri = ("urn:wx:design:layout-reuse:region#/definitions/record" if kind == "region"
           else f"urn:wx:design:layout-reuse:layout#/definitions/{kind}")
    validator = Draft7Validator({"$ref": uri}, registry=registry["registry"],
                                format_checker=registry["checker"])
    _require(isinstance(record, dict) and validator.is_valid(record),
             "已选布局资料结构无效，请重新核验")
    _require(record["sha256"] == content_sha(
        {key: value for key, value in record.items() if key != "sha256"}),
        "已选布局资料摘要不符，禁止使用")


def _reference(record, key):
    return {"id": record[key], "version": record["version"], "sha256": record["sha256"]}


def _pixel_region(rect, width, height):
    try:
        left, top, right, bottom = _pixel_rect(rect, width, height)
    except ValueError as exc:
        raise LayoutObservationError(
            "屏幕区域无效或越界，禁止截断", "item_outside_screen") from exc
    return {"x1": left, "y1": top, "x2": right, "y2": bottom,
            "frameW": width, "frameH": height}


class LayoutObservationService:
    def __init__(self, services_provider, device_provider):
        self._services = services_provider
        self._device = device_provider

    def execution_binding(self, device_id):
        services = self._services()
        pointer = services["state_store"].adaptation_pointer_get(device_id)
        if pointer is None:
            _require(not services["kv_store"].load(f"adaptation_pointer:{device_id}"),
                     "适配指针尚未完成可信迁移，禁止回退旧档案")
            return None
        device = self._device(device_id)
        return {"adaptation_pointer": copy.deepcopy(pointer),
                "region_pointer": dict(services["region_store"].get_pointer(device_id) or {}),
                "device_binding": {key: device.get(key) for key in BINDING_KEYS}}

    def resolve(self, device_id):
        """None means no selected shared layout; all selected errors are terminal."""
        try:
            services = self._services()
            state, ops = services["state_store"], services["ops_svc"]
            pointer = state.adaptation_pointer_get(device_id)
            if pointer is None:
                _require(not services["kv_store"].load(f"adaptation_pointer:{device_id}"),
                         "适配指针尚未完成可信迁移，禁止回退旧档案")
                return None
            _require(type(pointer.get("version")) is int and pointer["version"] > 0
                     and type(pointer.get("revision")) is int and pointer["revision"] > 0,
                     "已选适配指针无效")
            adaptation = ops.resolve_adaptation(device_id, pointer["version"])
            _validate(adaptation, "adaptation")
            _require(_reference(adaptation, "device_id") == {
                "id": device_id, "version": pointer["version"], "sha256": pointer["sha256"]},
                "已选适配与当前指针不一致")
            ref = adaptation["template_ref"]
            template = ops.resolve_template(ref["id"], ref["version"])
            _validate(template, "template")
            _require(_reference(template, "template_id") == ref, "适配引用的模板已损坏")
            region_pointer = services["region_store"].get_pointer(device_id)
            region_ref = adaptation["installation_region_ref"]
            _require(region_pointer is not None and region_ref == {
                "id": device_id, "version": region_pointer["selected_version"],
                "sha256": region_pointer["selected_sha256"]},
                "粗框已变更，适配需要重新核验", "source_binding_changed")
            region = ops.resolve_region(device_id, region_ref["version"])
            _validate(region, "region")
            _require(_reference(region, "device_id") == region_ref, "当前粗框资料已损坏")
            assets = {}
            for item in template["assets"]:
                _require(item["id"] not in assets, "模板资产编号重复")
                data = ops.resolve_template_asset(ref["id"], ref["version"], item["id"])
                _require(data and hashlib.sha256(data).hexdigest() == item["sha256"],
                         "模板特征资产缺失或摘要不符")
                image = screen_proof.decode(data)
                _require(image.shape[:2] == (item["height"], item["width"]),
                         "模板特征资产尺寸不符")
                assets[item["id"]] = data
            empty_assets = {}
            for sample in adaptation["color_samples"]:
                asset = sample["empty_input_asset_ref"]
                if asset:
                    data = state.read_adaptation_asset(device_id, pointer["version"],
                                                        asset["id"] + ".png")
                    if data is None:
                        data = state.read_adaptation_asset(device_id, pointer["version"], asset["id"])
                    _require(data and hashlib.sha256(data).hexdigest() == asset["sha256"],
                             "空输入参考资产缺失或摘要不符")
                    empty_assets[sample["state"]] = screen_proof.decode(data)
            snapshot = SelectedLayout(self, device_id, pointer, region_pointer, adaptation,
                                      template, region, assets, empty_assets)
            snapshot.ensure_current()
            return snapshot
        except LayoutObservationError:
            raise
        except Exception as exc:
            raise LayoutObservationError("已选布局资料不可读取或已损坏，禁止回退旧档案") from exc


class SelectedLayout:
    def __init__(self, service, device_id, pointer, region_pointer, adaptation,
                 template, region, assets, empty_assets):
        self.service, self.device_id = service, device_id
        self.pointer, self.region_pointer = copy.deepcopy(pointer), dict(region_pointer)
        self.adaptation, self.template, self.region = adaptation, template, region
        self.assets, self.empty_assets = assets, empty_assets
        current = self.service._device(device_id)
        self.device_binding = {key: current.get(key) for key in BINDING_KEYS}
        self.pages = {p["state"]: copy.deepcopy(p) for p in template["pages"]}
        self.checks = {p["state"]: p for p in adaptation["page_checks"]}
        self.colors = {p["state"]: p for p in adaptation["color_samples"]}
        _require(all(len(rows) == 4 and set(rows) == set(STATES)
                     for rows in (self.pages, self.checks, self.colors)),
                 "布局四态定义不完整或重复")
        self._apply_overrides()

    def _apply_overrides(self):
        seen = set()
        for override in self.adaptation["local_overrides"]:
            key = override["state"], override["kind"], override["item_id"]
            _require(key not in seen, "局部覆盖重复")
            seen.add(key)
            _require(override["kind"] in {"target", "region"},
                     "锚点覆盖尚未具有可验证的运行时定位语义，请重新核验")
            page = self.pages[override["state"]]
            item = next((i for i in page[override["kind"] + "s"]
                         if i["id"] == override["item_id"]), None)
            _require(item is not None and any(a["id"] == override["anchor_id"]
                                              for a in page["anchors"]), "局部覆盖依赖不存在")
            item.update(anchor_id=override["anchor_id"],
                        relative_rect=copy.deepcopy(override["relative_rect"]))

    def ensure_current(self):
        parts = self.service._services()
        _require(parts["state_store"].adaptation_pointer_get(self.device_id) == self.pointer,
                 "识别过程中适配版本已变化，请重新观测", "source_binding_changed")
        _require(dict(parts["region_store"].get_pointer(self.device_id) or {}) == self.region_pointer,
                 "识别过程中粗框已变化，请重新观测", "source_binding_changed")
        current = self.service._device(self.device_id)
        _require({key: current.get(key) for key in BINDING_KEYS} == self.device_binding,
                 "识别过程中设备绑定已变化，请重新观测", "source_binding_changed")

    def verify_saved_binding(self, bundle):
        """Validate persisted readiness only; never capture, match or mutate."""
        _require(isinstance(bundle, dict) and bundle.get("status") in {"ready", "motion_ready"}
                 and bundle.get("deviceId") == self.device_id,
                 "本设备当前校准尚未就绪", "geometry_changed")
        current = self.service._device(self.device_id)
        expected = self.region["installation_binding"]
        _require(bundle.get("schemaVersion") == 3
                 and (bundle.get("camera") or {}).get("validated") is True,
                 "共享布局识别需要有效 v3 相机校准", "geometry_changed")
        _require(current.get("ime_device_id") == self.adaptation["ime_device_id"]
                 and (current.get("daemon_binding_id") or current.get("arm_binding_id"))
                 == expected["arm_binding_id"], "手机或机械臂绑定已变化", "source_binding_changed")
        for saved, field in (
            ("camera_id", "cameraId"),
            ("camera_device_unique_id", "cameraDeviceUniqueId"),
            ("camera_binding_id", "cameraBindingId"),
        ):
            _require(expected[saved] == bundle.get(field)
                     and (not current.get(saved) or current[saved] == expected[saved]),
                     "适配、校准与当前相机来源不一致", "source_binding_changed")
        _require(self.region["source_geometry_id"] == bundle.get("sourceGeometryId")
                 and (not current.get("source_geometry_id") or current["source_geometry_id"]
                      == self.region["source_geometry_id"]),
                 "相机来源几何已变化", "source_binding_changed")
        descriptor = self.adaptation["screen_descriptor"]
        try:
            matrix, width, height = matrix_and_size(bundle)
        except LayoutGeometryError as exc:
            raise LayoutObservationError("本台屏幕投影矩阵或尺寸无效", "geometry_changed") from exc
        _require((descriptor["width_px"], descriptor["height_px"]) == (width, height)
                 and descriptor["calibration_geometry_hash"] == (
                     bundle.get("coordinateFrameHash") or bundle.get("bindingFingerprint")),
                 "屏幕几何已变化，适配需要重新核验", "geometry_changed")
        _require(self.region["source_size"] == bundle.get("frame"),
                 "粗框来源尺寸与当前相机不一致", "source_binding_changed")
        try:
            screen_pose.validate_corners(
                (bundle.get("screen", {}).get("edgeModel") or {}).get("corners"))
        except ValueError as exc:
            raise LayoutObservationError(
                "本台 v3 缺少可核验的屏幕边界，不能使用运行模板", "geometry_changed") from exc
        for state, page in self.pages.items():
            check = self.checks[state]
            _require(check["status"] != "passed" or all(
                item["status"] == "passed"
                for item in check["anchor_results"] + check["item_results"]),
                "页面总体通过与逐项安全核验结果矛盾")
            self._verify_target_safety(page, check, bundle, width, height)
        return matrix, width, height

    def _verify_page_evidence(self, state, width, height):
        page, check = self.pages[state], self.checks[state]
        purposes = [item["purpose"] for item in page["regions"]]
        required = ({"unread", "content", "foreground", "background"}
                    if state == "wechat_list" else
                    {"title", "input", "content", "foreground", "background"})
        _require(required <= set(purposes) and len(purposes) == len(set(purposes)),
                 "页面缺少明确用途区域或用途重复，请重新登记")
        for item in page["regions"]:
            _require(self.item_passed(state, "region", item["id"], check),
                     "当前页面的观察区域尚未通过复核")
        sample = self.colors[state]
        _require(np.linalg.norm(np.array(sample["foreground_rgb"], dtype=float)
                               - sample["background_rgb"]) >= 60,
                 "内容前景色与背景色过于接近")
        if state != "wechat_list":
            reference_state = "chat_ime_empty" if state == "chat_ime_text" else state
            reference = self.empty_assets.get(reference_state)
            _require(reference is not None, "页面缺少可信空输入参考")
            input_id = next(r["id"] for r in page["regions"] if r["purpose"] == "input")
            item = next(r for r in check["item_results"]
                        if r["kind"] == "region" and r["id"] == input_id)
            rect = _pixel_region(item["resolved_rect"], width, height)
            _require(reference.shape == (rect["y2"] - rect["y1"],
                                          rect["x2"] - rect["x1"], 3),
                     "空输入参考图尺寸与已发布输入区域不一致")

    def item_passed(self, state, kind, item_id, check):
        definition = [item for item in self.pages[state][kind + "s"] if item["id"] == item_id]
        results = [item for item in check["item_results"]
                   if item["kind"] == kind and item["id"] == item_id]
        if len(definition) != 1 or len(results) != 1:
            return False
        anchors = [a for a in check["anchor_results"] if a["id"] == definition[0]["anchor_id"]]
        return (check["status"] != "unsupported" and results[0]["status"] == "passed"
                and results[0]["resolved_rect"] is not None and len(anchors) == 1
                and anchors[0]["status"] == "passed" and anchors[0]["matched_rect"] is not None)

    def _page_anchors_passed(self, state, check):
        required = {a["id"] for a in self.pages[state]["anchors"] if a["required"]}
        results = {a["id"]: a for a in check["anchor_results"]}
        return (check["status"] != "unsupported" and len(required) >= 2
                and all(results.get(key, {}).get("status") == "passed" for key in required))

    @staticmethod
    def _verify_target_safety(page, check, bundle, width, height):
        anchor_definitions = {item["id"]: item for item in page["anchors"]}
        anchors = {item["id"]: item for item in check["anchor_results"]}
        _require(len(anchor_definitions) == len(page["anchors"])
                 == len(anchors) == len(check["anchor_results"])
                 and set(anchors) == set(anchor_definitions)
                 and all(item["status"] != "passed" or item["matched_rect"] is not None
                         for item in anchors.values()),
                 "已发布安全核验的锚点缺失、重复或状态矛盾")
        _require(all(item["safe_rect"] is None
                     for item in check["item_results"]
                     if item["kind"] == "region" or item["status"] != "passed"),
                 "观察区域或失败项目不能携带点击安全区")
        definitions = {item["id"]: item for item in page["targets"]}
        targets = [item for item in check["item_results"] if item["kind"] == "target"]
        _require(len(definitions) == len(page["targets"]) == len(targets)
                 and {item["id"] for item in targets} == set(definitions),
                 "已发布目标缺少唯一的安全核验结果")
        _require(all(item["status"] != "passed" or item["resolved_rect"] is not None
                     for item in targets), "已发布目标缺少安全定位结果")
        _require(all(definition["anchor_id"] in anchors for definition in definitions.values()),
                 "已发布目标缺少安全定位依赖")
        recalculated = apply_target_safety(
            copy.deepcopy(check), page, (width, height), mapping_error_bound(bundle))
        expected = {item["id"]: item for item in recalculated["item_results"]
                    if item["kind"] == "target"}
        for item in targets:
            if item["status"] != "passed":
                continue
            dependency = anchors[definitions[item["id"]]["anchor_id"]]
            _require(dependency["status"] == "passed" and dependency["matched_rect"] is not None,
                     "已发布目标与所属锚点的通过状态矛盾")
            safe = expected[item["id"]]["safe_rect"]
            actual = item["safe_rect"]
            _require(expected[item["id"]]["status"] == "passed",
                     "已发布目标无法根据当前误差生成安全区")
            if definitions[item["id"]]["mode"] == "tap":
                _require(safe is not None and actual is not None
                         and all(abs(actual[key] - safe[key]) <= 1e-12 for key in safe),
                         "已发布点击目标的安全区缺失或与当前误差不符")
            else:
                _require(actual is None, "悬停目标不能携带点击安全区")

    def metadata(self, bundle):
        _, width, height = self.verify_saved_binding(bundle)
        for state in STATES:
            _require(self._page_anchors_passed(state, self.checks[state]),
                     "部分页面的识别锚点尚未通过复核")
            self._verify_page_evidence(state, width, height)
        return {"registered": True, "usable": True,
                "reference_states": ["conversation", "wechat_main"],
                "regions_registered": True, "revision": self.pointer["revision"],
                "blocking_reasons": [], **self.references}

    def _input_state(self, state, check, screen, engine, cache):
        """Distinguish keyboard states with the same pixels used by the proof."""
        _require(engine is not None, "缺少当前输入区域视觉识别，不能区分空和有字", "wrong_page")
        regions = self.pages[state]["regions"]
        input_id = next(r["id"] for r in regions if r["purpose"] == "input")
        _require(self.item_passed(state, "region", input_id, self.checks[state]),
                 "输入区域尚未通过本台复核，不能区分键盘页面", "wrong_page")
        items = [item for item in check["item_results"] if item["kind"] == "region"
                 and item["id"] == input_id and item["status"] == "passed"]
        _require(len(items) == 1, "当前输入区域不唯一，不能判断页面", "wrong_page")
        height, width = screen.shape[:2]
        rect = _pixel_region(items[0]["resolved_rect"], width, height)
        patch = screen_proof.crop(screen, rect)
        reference = self.empty_assets.get("chat_ime_empty")
        _require(reference is not None and reference.shape == patch.shape,
                 "当前输入框尺寸不受已登记空白参考支持", "unsupported_layout")
        key = tuple(rect[name] for name in ("x1", "y1", "x2", "y2"))
        if key not in cache:
            try:
                cache[key] = screen_proof.recognize_patch(engine, patch, "input")
            except (ValueError, RuntimeError) as exc:
                raise LayoutObservationError("当前输入区域 OCR 无法核验", "wrong_page") from exc
        ocr = cache[key]
        visually_empty = screen_proof.input_is_empty(patch, reference, "")
        text = ocr["text"].strip()
        if visually_empty and not text:
            return "chat_ime_empty", ocr
        reliable_text = (text and ocr["confidence"] >= PASS_SCORE and ocr.get("words")
                         and all(word["confidence"] >= PASS_SCORE for word in ocr["words"]))
        if not visually_empty and reliable_text:
            return "chat_ime_text", ocr
        raise LayoutObservationError("输入区域像素与文字证据不一致或不充分，无法判断页面", "wrong_page")

    def prepare(self, image, frame, bundle, pose, calibration_binding, *, ocr_engine=None):
        """Use raw full-resolution v3 warp only, after the existing pose gate."""
        matrix, width, height = self.verify_saved_binding(bundle)
        expected = self.region["installation_binding"]
        for saved, live in (("camera_id", "camera_id"),
                            ("camera_device_unique_id", "device_unique_id"),
                            ("camera_binding_id", "camera_binding_id")):
            _require(expected[saved] == frame.get(live),
                     "当前帧相机来源与适配不一致", "source_binding_changed")
        _require(self.region["source_geometry_id"] == frame.get("source_geometry_id"),
                 "当前帧来源几何已变化", "source_binding_changed")
        cw, ch = frame["width"], frame["height"]
        rect = _pixel_region(self.region["rect"], cw, ch)
        corners = np.asarray(pose["corners"])
        _require(np.all(corners >= [rect["x1"], rect["y1"]])
                 and np.all(corners <= [rect["x2"], rect["y2"]]),
                 "当前屏幕超出已选粗框", "screen_outside_region")
        inverse = np.linalg.inv(matrix)
        points = np.array([[0, 0, 1], [width - 1, 0, 1], [width - 1, height - 1, 1],
                           [0, height - 1, 1]], dtype=float) @ inverse.T
        _require(np.isfinite(points).all() and (
            np.all(points[:, 2] > 1e-10) or np.all(points[:, 2] < -1e-10)),
            "屏幕反投影无效", "geometry_changed")
        points = points[:, :2] / points[:, 2:]
        _require(np.all(points >= [0, 0]) and np.all(points <= [cw - 1, ch - 1]),
                 "屏幕摆正范围超出原相机帧", "item_outside_screen")
        support = np.zeros((ch, cw), np.uint8)
        cv2.fillConvexPoly(support, np.rint(bundle["screen"]["edgeModel"]["corners"]).astype(np.int32), 255)
        support = cv2.warpPerspective(support, matrix, (width, height))
        _require(np.all(support == 255), "屏幕摆正包含无效边界像素", "item_outside_screen")
        screen = cv2.warpPerspective(image, matrix, (width, height), flags=cv2.INTER_LINEAR)
        encoded = cv2.imencode(".png", screen)[1].tobytes()
        matches = []
        page_scores = {}
        input_reads = {}
        selected_input = {}
        contradicted_states = set()
        for state, page in self.pages.items():
            check = compute_page_check(page, encoded,
                {a["id"]: self.assets.get(a["asset_id"]) for a in page["anchors"]}, [],
                mapping_error_bound_px=mapping_error_bound(bundle))
            required_ids = {a["id"] for a in page["anchors"] if a["required"]}
            page_scores[state] = min((a["best_score"] or 0 for a in check["anchor_results"]
                                      if a["id"] in required_ids), default=0)
            if (self._page_anchors_passed(state, self.checks[state])
                    and self._page_anchors_passed(state, check)):
                if state in ("chat_ime_empty", "chat_ime_text"):
                    observed_state, input_ocr = self._input_state(
                        state, check, screen, ocr_engine, input_reads)
                    if state != observed_state:
                        contradicted_states.add(state)
                        continue
                    if state == "chat_ime_text":
                        _require(any(anchor["id"] == "send_button_feature"
                                     and anchor["status"] == "passed"
                                     for anchor in check["anchor_results"]),
                                 "有字页面缺少当次发送按钮特征", "wrong_page")
                    selected_input[state] = input_ocr
                score = page_scores[state]
                matches.append((score, state, check))
        matches.sort(key=lambda row: row[0], reverse=True)
        _require(matches, "当前画面未通过已选模板匹配", "score_below_threshold")
        _require(not contradicted_states or any(
            candidate[1] in ("chat_ime_empty", "chat_ime_text") for candidate in matches),
            "当前键盘状态未完整核验，禁止回退其他页面", "wrong_page")
        _require(len(matches) == 1 and matches[0][0] - max(
            (score for state, score in page_scores.items()
             if state != matches[0][1] and state not in contradicted_states), default=0
        ) >= PASS_MARGIN,
                 "当前页面匹配存在歧义", "anchor_ambiguous")
        score, state, check = matches[0]
        profile = {"revision": self.pointer["revision"], "regions": {},
                   "binding": content_sha({"source": "shared_layout.v1",
                       "calibration_binding": calibration_binding, **self.references})}
        page = self.pages[state]
        definitions = {r["id"]: r for r in page["regions"]}
        for item in check["item_results"]:
            if (item["kind"] == "region" and item["status"] == "passed"
                    and self.item_passed(state, "region", item["id"], self.checks[state])):
                purpose = definitions[item["id"]]["purpose"]
                _require(purpose not in profile["regions"], "同用途区域重复，不能选取首项")
                profile["regions"][purpose] = _pixel_region(item["resolved_rect"], width, height)
        ui_state = "wechat_main" if state == "wechat_list" else "conversation"
        self.ensure_current()
        return LayoutFrame(self, frame, screen, inverse, profile, state, content_sha(bundle),
                           {"ui_state": ui_state, "ui_state_display":
                            "微信主界面" if state == "wechat_list" else "聊天界面",
                            "confidence": round(score, 3), "profile_revision": profile["revision"]},
                           input_ocr=selected_input.get(state), check=check, pose=pose)

    @property
    def execution_binding(self):
        return {"adaptation_pointer": copy.deepcopy(self.pointer),
                "region_pointer": dict(self.region_pointer),
                "device_binding": copy.deepcopy(self.device_binding)}

    @property
    def references(self):
        return {"adaptation_ref": _reference(self.adaptation, "device_id"),
                "template_ref": self.adaptation["template_ref"],
                "installation_region_ref": self.adaptation["installation_region_ref"]}


class LayoutFrame:
    def __init__(self, selected, source, image, inverse, profile, state, calibration_sha, ui,
                 *, input_ocr=None, check=None, pose=None):
        self.selected, self.source, self.image = selected, source, image
        self.inverse, self.profile, self.state, self.ui = inverse, profile, state, ui
        self.calibration_sha = calibration_sha
        self.input_ocr = input_ocr
        self.check, self.pose = check, pose

    def ensure_current(self, bundle):
        _require(content_sha(bundle) == self.calibration_sha,
                 "识别过程中校准已变化，请重新观测", "geometry_changed")
        self.selected.ensure_current()

    def target(self, target_id, target_ref=None):
        """Resolve a verified item in this frame, never a persisted click point."""
        target_ref = target_ref or {}
        for key, expected in (
            ("template_version", self.selected.template["version"]),
            ("adaptation_revision", self.selected.pointer["revision"]),
        ):
            if target_ref.get(key) is not None:
                _require(type(target_ref[key]) is int and target_ref[key] == expected,
                         "任务引用的模板或本台适配版本已变化", "source_binding_changed")
        for key, expected in self.selected.references.items():
            if key in target_ref:
                _require(target_ref[key] == expected,
                         "任务引用的布局资料已变化", "source_binding_changed")
        for check in (self.selected.checks[self.state], self.check):
            _require(self.selected.item_passed(self.state, "target", target_id, check),
                     f"当前页面目标 {target_id} 未通过本台复核或当次定位")
        definition = next(item for item in self.selected.pages[self.state]["targets"]
                          if item["id"] == target_id)
        result = next(item for item in self.check["item_results"]
                      if item["kind"] == "target" and item["id"] == target_id)
        rect = result["safe_rect"] if definition["mode"] == "tap" else result["resolved_rect"]
        _require(rect is not None, "当前点击目标缺少安全区")
        height, width = self.image.shape[:2]
        point = self.inverse @ np.array([
            (rect["x"] + rect["width"] / 2) * width,
            (rect["y"] + rect["height"] / 2) * height, 1])
        _require(np.isfinite(point).all() and abs(point[2]) > 1e-10,
                 "目标中心无法反投影", "geometry_changed")
        cx, cy = point[:2] / point[2]
        _require(0 <= cx < self.source["width"] and 0 <= cy < self.source["height"],
                 "目标中心超出相机范围", "item_outside_screen")
        return {"nx": float(cx / self.source["width"]), "ny": float(cy / self.source["height"]),
                "targetMode": definition["mode"], "target_source": "shared_layout",
                "coordinate_version": "calibrated_camera.v1",
                "frame_seq": self.source["source_frame_seq"],
                "frame_sha256": self.source.get("image_sha256"),
                "layout_state": self.state, **self.selected.references}

    def verify_target_point(self, target_id, resolved, bundle):
        """The already planned robot point must remain inside the live safe box."""
        from drivers import calibration_bundle as cb

        self.ensure_current(bundle)
        self.target(target_id)
        definition = next(item for item in self.selected.pages[self.state]["targets"]
                          if item["id"] == target_id)
        result = next(item for item in self.check["item_results"]
                      if item["kind"] == "target" and item["id"] == target_id)
        rect = result["safe_rect"] if definition["mode"] == "tap" else result["resolved_rect"]
        sx, sy = cb.screen_from_robot(bundle["xy"]["robotToScreen"],
                                      resolved["robotX"], resolved["robotY"])
        point = self.inverse @ np.array([sx, sy, 1])
        point = np.linalg.inv(np.asarray(self.pose["matrix"], dtype=float)) @ point
        matrix, width, height = matrix_and_size(bundle)
        point = matrix @ point
        _require(np.isfinite(point).all() and abs(point[2]) > 1e-10,
                 "机械落点无法回投影核验", "geometry_changed")
        x, y = point[:2] / point[2] / [width, height]
        _require(rect["x"] < x < rect["x"] + rect["width"]
                 and rect["y"] < y < rect["y"] + rect["height"],
                 "走位后的目标安全区已变化，禁止沿用旧落点", "geometry_changed")

    def evidence_profile(self):
        height, width = self.image.shape[:2]
        self.selected._verify_page_evidence(self.state, width, height)
        profile = self.profile
        if self.state == "wechat_list":
            return profile
        regions = profile["regions"]
        _require({"title", "input", "content", "foreground", "background"} <= regions.keys(),
                 "当前页面缺少标题、输入、内容或明确用途的颜色区域")
        sample = self.selected.colors[self.state]
        colors = {}
        for purpose in ("foreground", "background"):
            color = np.asarray(sample[purpose + "_rgb"], dtype=float)[::-1]
            # Samples are registration facts, not fixed live bubble locations.
            # Current content may scroll; actual components still use the
            # existing strict color-distance/clearance/clipping checks.
            colors[purpose + "_sample"] = color.tolist()
        _require(np.linalg.norm(np.array(colors["foreground_sample"])
                               - colors["background_sample"]) >= 60,
                 "内容前景色与背景色过于接近")
        reference_state = "chat_ime_empty" if self.state == "chat_ime_text" else self.state
        reference = self.selected.empty_assets.get(reference_state)
        _require(reference is not None, "当前页面没有可信空输入参考")
        actual = screen_proof.crop(self.image, regions["input"])
        _require(reference.shape == actual.shape, "空输入参考图尺寸与实际输入区域不一致")
        profile.update(colors=colors, empty_input=screen_proof.encode(reference))
        return profile

    def camera_point(self, x, y):
        point = self.inverse @ np.array([x, y, 1], dtype=float)
        _require(np.isfinite(point).all() and abs(point[2]) > 1e-10,
                 "观测坐标无法反投影", "geometry_changed")
        cx, cy = point[:2] / point[2]
        _require(0 <= cx < self.source["width"] and 0 <= cy < self.source["height"],
                 "观测坐标超出相机范围", "item_outside_screen")
        return float(cx), float(cy)

    def camera_rect(self, x, y, width, height):
        points = np.array([[x, y, 1], [x + width, y, 1],
                           [x + width, y + height, 1], [x, y + height, 1]],
                          dtype=float) @ self.inverse.T
        _require(np.isfinite(points).all() and (
            np.all(points[:, 2] > 1e-10) or np.all(points[:, 2] < -1e-10)),
            "观测坐标无法反投影", "geometry_changed")
        points = points[:, :2] / points[:, 2:]
        _require(np.all(points >= 0)
                 and np.all(points <= [self.source["width"], self.source["height"]]),
                 "观测坐标超出相机范围", "item_outside_screen")
        left, top = points.min(axis=0)
        right, bottom = points.max(axis=0)
        return {"x": float(left), "y": float(top),
                "width": float(right - left), "height": float(bottom - top)}
