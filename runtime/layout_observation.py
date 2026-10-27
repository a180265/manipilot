"""Read-only selected layout resolution and single-frame visual observation."""
from __future__ import annotations

import copy
import hashlib
import math
import os

import cv2
import numpy as np

from drivers import screen_pose, screen_proof
from runtime.layout_geometry import LayoutGeometryError, mapping_error_bound, matrix_and_size
from runtime.layout_matching import (
    compute_page_check, resolve_page_check, PASS_MARGIN, PASS_SCORE, _pixel_rect,
)
from runtime.layout_state import content_sha
from runtime.layout_pages import LEGACY_STATES, is_text_page, page_states, page_ui


STATES = LEGACY_STATES
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
            _require(region_pointer is not None, "已选布局缺少生效粗框", "source_binding_changed")
            region_ref = {
                "id": device_id, "version": region_pointer["selected_version"],
                "sha256": region_pointer["selected_sha256"]}
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
            # Only an exact completed-calibration receipt can join old page
            # assets to a redrawn region; merely publishing it is not enough.
            _require(snapshot.adaptation["installation_region_ref"] == region_ref,
                     "粗框已变更，适配需要重新核验", "source_binding_changed")
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
        # A completed new calibration may reuse this exact immutable selection.
        # Only runtime projection/source copies change; old evidence is untouched.
        from drivers import calibration_bundle as cb
        from runtime.layout_recalibration import derive_execution_layout
        services = service._services()
        root = services.get("calibration_data_root", services["state_store"].data_root)
        bundle = cb.load_bundle(device_id, root)
        self.recalibration_fingerprint = None
        if bundle:
            derived = derive_execution_layout(root, bundle, adaptation, region)
            if derived:
                self.adaptation, self.region = derived
                self.recalibration_fingerprint = cb.motion_calibration_fingerprint(bundle)
        self.assets, self.empty_assets = assets, empty_assets
        current = self.service._device(device_id)
        self.device_binding = {key: current.get(key) for key in BINDING_KEYS}
        self.pages = {p["state"]: copy.deepcopy(p) for p in template["pages"]}
        self.checks = {p["state"]: p for p in adaptation["page_checks"]}
        self.colors = {p["state"]: p for p in adaptation["color_samples"]}
        try:
            states = page_states(template)
        except ValueError as exc:
            raise LayoutObservationError(str(exc)) from exc
        _require(all(len(original) == len(rows) == len(states)
                     and set(rows) == set(states)
                     for original, rows in ((template["pages"], self.pages),
                         (adaptation["page_checks"], self.checks),
                         (adaptation["color_samples"], self.colors))),
                 "布局页面、复核结果或颜色记录不完整或重复")
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
            if "tapDepth" in override:
                from drivers.daemon_protocol import check_tap
                _require(override["kind"] == "target" and item.get("mode") == "tap",
                         "接触深度覆盖只允许点击目标")
                check_tap(override["tapDepth"])
                item["tapDepth"] = override["tapDepth"]
            if "tapDwellMs" in override:
                dwell = override["tapDwellMs"]
                _require(override["kind"] == "target" and item.get("mode") == "tap"
                         and type(dwell) is int and 0 <= dwell <= 2000,
                         "接触停留仅允许点击目标，且须为 0-2000 毫秒整数")
                item["tapDwellMs"] = dwell

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
        if self.recalibration_fingerprint is not None:
            from drivers import calibration_bundle as cb
            _require(cb.motion_calibration_fingerprint(bundle) == self.recalibration_fingerprint,
                     "布局复用所依据的新校准已变化", "geometry_changed")
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
            self._verify_target_safety(page, check, width, height)
        return matrix, width, height

    def _verify_page_evidence(self, state, width, height):
        page, check = self.pages[state], self.checks[state]
        purposes = [item["purpose"] for item in page["regions"]]
        # List pages may derive their palette from the two verified anchors.
        # Message bubbles still require explicit foreground/background regions.
        required = ({"input", "content"} if is_text_page(page) else
                    {"unread", "content"} if state == "wechat_list" else
                    {"title", "input", "content", "foreground", "background"})
        _require(required <= set(purposes) and len(purposes) == len(set(purposes)),
                 "页面缺少明确用途区域或用途重复，请重新登记")
        for item in page["regions"]:
            _require(self.item_passed(state, "region", item["id"], check),
                     "当前页面的观察区域尚未通过复核")
        if is_text_page(page):
            return
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
    def _verify_target_safety(page, check, width, height):
        """Check archived evidence structure without substituting a newer calibration.

        Archived boxes describe the calibration used at publication. They never
        authorize a current click; prepare() resolves that box with today's bound.
        """
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
        for item in targets:
            if item["status"] != "passed":
                continue
            dependency = anchors[definitions[item["id"]]["anchor_id"]]
            _require(dependency["status"] == "passed" and dependency["matched_rect"] is not None,
                     "已发布目标与所属锚点的通过状态矛盾")
            actual = item["safe_rect"]
            if definitions[item["id"]]["mode"] == "tap":
                resolved = item["resolved_rect"]
                _require(actual is not None, "已发布点击目标的安全区缺失")
                for rect in (resolved, actual):
                    _require(all(math.isfinite(rect[key]) for key in
                                 ("x", "y", "width", "height"))
                             and rect["x"] >= 0 and rect["y"] >= 0
                             and rect["width"] > 0 and rect["height"] > 0
                             and rect["x"] + rect["width"] <= 1 + 1e-12
                             and rect["y"] + rect["height"] <= 1 + 1e-12,
                             "已发布点击目标的安全区或定位范围无效")
                margins = (
                    (actual["x"] - resolved["x"]) * width,
                    (resolved["x"] + resolved["width"]
                     - actual["x"] - actual["width"]) * width,
                    (actual["y"] - resolved["y"]) * height,
                    (resolved["y"] + resolved["height"]
                     - actual["y"] - actual["height"]) * height,
                )
                anchor_error = dependency.get("localization_error_bound_px")
                _require(type(anchor_error) in (int, float)
                         and math.isfinite(anchor_error) and anchor_error >= 0,
                         "已发布点击目标的锚点安全误差无效")
                minimum = max(2, math.ceil(anchor_error))
                _require(all(abs(margin - margins[0]) <= 1e-7
                             and abs(margin - round(margin)) <= 1e-7
                             and margin >= minimum - 1e-7 for margin in margins),
                         "已发布点击目标的安全区不是有效的等距内缩范围")
            else:
                _require(actual is None, "悬停目标不能携带点击安全区")

    def metadata(self, bundle):
        _, width, height = self.verify_saved_binding(bundle)
        for state in self.pages:
            _require(self._page_anchors_passed(state, self.checks[state]),
                     "部分页面的识别锚点尚未通过复核")
            self._verify_page_evidence(state, width, height)
        return {"registered": True, "usable": True,
                "reference_states": sorted({page_ui(page)[0]
                                            for page in self.pages.values()}),
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

    def _prepare_geometry(self, image, frame, bundle, pose):
        """Validate the saved geometry and source without asserting a UI page."""
        matrix, width, height = self.verify_saved_binding(bundle)
        inverse = np.linalg.inv(matrix)
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
        from runtime.layout_geometry import LayoutGeometryError, warp_root_from_raw
        try:
            screen = warp_root_from_raw(image, bundle, pose, self.region)
        except LayoutGeometryError as exc:
            raise LayoutObservationError(str(exc), "item_outside_screen") from exc
        self.ensure_current()
        return screen, inverse, width, height

    def prepare_text(self, image, frame, bundle, pose, calibration_binding):
        """Exact OCR geometry; deliberately has no page or fixed-target authority."""
        screen, inverse, _, _ = self._prepare_geometry(image, frame, bundle, pose)
        return LayoutTextFrame(self, frame, screen, inverse, content_sha(bundle), pose)

    def prepare(self, image, frame, bundle, pose, calibration_binding, *, ocr_engine=None, observed_state=None):
        """Use raw full-resolution v3 warp only, after the existing pose gate."""
        screen, inverse, width, height = self._prepare_geometry(image, frame, bundle, pose)
        selected_input = {}
        if os.environ.get("ARMFLEET_RUNTIME_VISION_MODE") == "calibrated" and observed_state:
            _require(observed_state in self.pages, "观察页面未登记", "wrong_page")
            state, score = observed_state, 1.0
            saved = self.checks[state]
            check = resolve_page_check(self.pages[state], copy.deepcopy(saved["anchor_results"]),
                saved["evidence_ids"], (width, height),
                mapping_error_bound_px=mapping_error_bound(bundle))
        else:
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
        from drivers.click_points import policy_from_bundle

        click_policy = policy_from_bundle(bundle)
        profile_binding = {"source": "shared_layout.v1",
                           "calibration_binding": calibration_binding, **self.references}
        # Some deployments intentionally keep legacy visual references across
        # recalibration. Active click policy must still invalidate effect proofs;
        # leave the absent-policy digest unchanged for those old references.
        if click_policy is not None:
            profile_binding["click_point_policy"] = click_policy
        profile = {"revision": self.pointer["revision"], "regions": {},
                   "binding": content_sha(profile_binding)}
        page = self.pages[state]
        definitions = {r["id"]: r for r in page["regions"]}
        for item in check["item_results"]:
            if (item["kind"] == "region" and item["status"] == "passed"
                    and self.item_passed(state, "region", item["id"], self.checks[state])):
                purpose = definitions[item["id"]]["purpose"]
                _require(purpose not in profile["regions"], "同用途区域重复，不能选取首项")
                profile["regions"][purpose] = _pixel_region(item["resolved_rect"], width, height)
        ui_state, ui_display = page_ui(page)
        self.ensure_current()
        return LayoutFrame(self, frame, screen, inverse, profile, state, content_sha(bundle),
                           {"ui_state": ui_state, "ui_state_display":
                            ui_display,
                            "confidence": round(score, 3), "profile_revision": profile["revision"]},
                           input_ocr=selected_input.get(state), check=check, pose=pose,
                           click_policy=click_policy)

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
                 *, input_ocr=None, check=None, pose=None, click_policy=None):
        self.selected, self.source, self.image = selected, source, image
        self.inverse, self.profile, self.state, self.ui = inverse, profile, state, ui
        self.calibration_sha = calibration_sha
        self.input_ocr = input_ocr
        self.check, self.pose = check, pose
        self.click_policy = copy.deepcopy(click_policy)

    def ensure_current(self, bundle):
        _require(content_sha(bundle) == self.calibration_sha,
                 "识别过程中校准已变化，请重新观测", "geometry_changed")
        self.selected.ensure_current()

    def target(self, target_id, target_ref=None, *, click_attempt=0):
        """Resolve a verified item in this frame, never a persisted click point."""
        from drivers.click_points import rectangle_point
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
        screen_point, retry_supported, safety = rectangle_point(
            target_id, rect, width, height, click_attempt,
            policy=self.click_policy if definition["mode"] == "tap" else None)
        retry_supported = retry_supported and definition["mode"] == "tap"
        _require(not click_attempt or retry_supported, "此目标不支持替代点击落点")
        point = self.inverse @ np.array([*screen_point, 1])
        _require(np.isfinite(point).all() and abs(point[2]) > 1e-10,
                 "目标中心无法反投影", "geometry_changed")
        cx, cy = point[:2] / point[2]
        _require(0 <= cx < self.source["width"] and 0 <= cy < self.source["height"],
                 "目标中心超出相机范围", "item_outside_screen")
        return {**{key: definition[key] for key in ("tapDepth", "tapDwellMs") if key in definition},
                "nx": float(cx / self.source["width"]), "ny": float(cy / self.source["height"]),
                "targetMode": definition["mode"], "target_source": "shared_layout",
                "coordinate_version": "calibrated_camera.v1",
                "frame_seq": self.source["source_frame_seq"],
                "frame_sha256": self.source.get("image_sha256"),
                "click_attempt": click_attempt, "click_retry_supported": retry_supported,
                "click_point": {"screen": screen_point.tolist(), "camera": [float(cx), float(cy)],
                                "safety": safety, **{key: safety[key] for key in
                                    ("originalCenter", "aim", "selectionOffset", "policy")
                                    if key in safety}},
                "layout_state": self.state, **self.selected.references}

    def verify_target_point(self, target_id, resolved, bundle):
        """The already planned robot point must remain inside the live safe box."""
        from drivers import calibration_bundle as cb

        self.ensure_current(bundle)
        selected = self.target(target_id)
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
        if (self.click_policy is not None and definition["mode"] == "tap"
                and selected["click_point"]["safety"]["preferenceSatisfied"]):
            insets = self.click_policy["preferredInsetsPx"]
            _require(insets["left"] <= x * width <= width - insets["right"]
                     and insets["top"] <= y * height <= height - insets["bottom"],
                     "走位后的落点超出本台屏幕内部偏好区", "geometry_changed")

    def evidence_profile(self):
        height, width = self.image.shape[:2]
        self.selected._verify_page_evidence(self.state, width, height)
        profile = self.profile
        if self.state == "wechat_list" or is_text_page(self.selected.pages[self.state]):
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


class LayoutTextFrame:
    """Calibrated screen geometry with no page state or fixed click targets."""

    localization_mode = "screen_text"

    def __init__(self, selected, source, image, inverse, calibration_sha, pose):
        self.selected, self.source, self.image = selected, source, image
        self.inverse, self.calibration_sha, self.pose = inverse, calibration_sha, pose

    ensure_current = LayoutFrame.ensure_current
    camera_point = LayoutFrame.camera_point
    camera_rect = LayoutFrame.camera_rect
