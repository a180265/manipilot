"""区域命中验收只依靠冻结几何和实际点；不连接真设备。"""
from copy import deepcopy
import math

import pytest

from drivers import calibration_bundle as cb
from tests.test_calibration_bundle import _ready_bundle, _gate, _codes


SCREEN = {"screenWidth": 1080, "screenHeight": 2400}
TARGET = {"u": .5, "v": .5, "widthU": .12, "heightV": .12}


def test_region_acceptance_uses_target_size_and_measured_contact():
    evidence = cb.calibration_region_evidence(TARGET, SCREEN, [570, 1200], [540, 1200])
    assert evidence["criterion"] == "target_region_v1"
    assert evidence["targetRect"] == TARGET
    assert evidence["boundsPx"] == pytest.approx({
        "left": 475.2, "top": 1056, "right": 604.8, "bottom": 1344})
    assert evidence["safetyInsetPx"] == pytest.approx(6.48)
    assert evidence["safeBoundsPx"] == pytest.approx({
        "left": 481.68, "top": 1062.48, "right": 598.32, "bottom": 1337.52})
    assert evidence["marginPx"] == pytest.approx(34.8)
    assert evidence["touchTargetErrorPx"] == 30
    assert evidence["cameraTouchGapPx"] == 30
    assert evidence["passed"] is True


def region_point(index=1, u=.5, v=.5, offset=30):
    rect = {"u": u, "v": v, "widthU": 2 * min(.06, .6*u, .6*(1-u)),
            "heightV": 2 * min(.06, .6*v, .6*(1-v))}
    evidence = cb.calibration_region_evidence(
        rect, SCREEN, [u*1080 + offset, v*2400], [u*1080, v*2400])
    return {"targetId": f"validation-{index}", "u": u, "v": v, "passed": True,
            "touchTargetErrorPx": evidence["touchTargetErrorPx"],
            "cameraTouchGapPx": evidence["cameraTouchGapPx"], "regionEvidence": evidence}


def region_bundle():
    bundle = _ready_bundle()
    bundle["status"] = cb.MOTION_READY_STATUS
    bundle["autoCalibration"] = {"state": "PROMOTED", "runId": "region-run"}
    bundle["validation"] = {"criterion": cb.REGION_VALIDATION_CRITERION,
        "passed": True, "runId": "region-run", "points": [region_point(i) for i in range(1, 6)]}
    bundle["modelLocks"] = {name: {"state": cb.MODEL_LOCK_LOCKED, "version": 1}
                            for name in ("cameraToScreen", "armAffine")}
    bundle["z"]["zSafe"] = 2
    bundle["safePark"] = {"confirmed": True, "parkX": -10, "parkY": -10, "parkZ": 2}
    bundle["camera"]["cameraToScreen"]["maxErrorPx"] = 2
    samples = []
    for index, (u, v) in enumerate((u,v) for v in (.1,.5,.9) for u in (.1,.5,.9)):
        samples.append({"targetId": str(index), "u": u, "v": v,
                        "targetRect": {**TARGET,"u": u,"v": v},
                        "effectiveRobotXY": [u*100, v*100], "actualTouch": [u*1080,v*2400]})
    bundle["xy"].update(samples=samples, residualRmsPx=4, maxResidualPx=7,
        modelSelection={"selectedModel": "affine", "sampleCount": 9,
            "affine": {"training": {"rmsPx": 4, "maxErrorPx": 7},
                       "leaveOneOut": {"rmsPx": 8, "maxErrorPx": 8, "residualsPx": [8]*9}}})
    return bundle


def test_formal_region_acceptance_allows_30px_inside_target_and_keeps_other_gates():
    bundle = region_bundle()
    assert cb.automatic_calibration_verified(bundle)
    assert _gate(bundle) == []
    bundle["safePark"]["confirmed"] = False
    assert not cb.automatic_calibration_verified(bundle)
    assert "motion_qualification_incomplete" in _codes(_gate(bundle))


@pytest.mark.parametrize("status", ["ready", cb.MOTION_READY_STATUS])
@pytest.mark.parametrize("criterion", ["future_mode", None, True])
def test_unknown_criterion_cannot_use_old_motion_fallback(status, criterion):
    bundle = region_bundle()
    bundle["status"] = status
    bundle["autoCalibration"] = {}
    bundle["validation"]["criterion"] = criterion
    bundle["validation"]["tolerancePx"] = 100
    assert not cb.automatic_calibration_verified(bundle)
    assert "calibration_validation_criterion_unknown" in _codes(_gate(bundle))


@pytest.mark.parametrize("touch,camera,expected", [
    ([598,1200],[540,1200],True), ([599,1200],[540,1200],False),
    ([540,1200],[599,1200],False), ([605,1200],[540,1200],False),
])
def test_safe_region_contains_both_contact_and_camera(touch,camera,expected):
    assert cb.calibration_region_evidence(TARGET,SCREEN,touch,camera)["passed"] is expected


@pytest.mark.parametrize("change", [
    {"widthU": .12001}, {"heightV": 0}, {"u": 0}, {"v": True},
    {"widthU": float("nan")}, {"heightV": "0.12"}, {"u": .01},
])
def test_region_rejects_invalid_or_enlarged_target(change):
    with pytest.raises(ValueError):
        cb.calibration_region_evidence({**TARGET,**change},SCREEN,[540,1200],[540,1200])


@pytest.mark.parametrize("value", [None, True, -1, 0, float("nan"), float("inf"), "1080"])
def test_region_rejects_invalid_screen_size(value):
    with pytest.raises(ValueError):
        cb.calibration_region_evidence(TARGET,{**SCREEN,"screenWidth":value},[540,1200])


def test_point_recomputes_evidence_and_does_not_trust_success_or_margin():
    point = region_point()
    point["regionEvidence"].update(passed=False, marginPx=-100, safeBoundsPx={})
    assert cb.calibration_region_point_passed(point, SCREEN)
    point["regionEvidence"]["actualTouch"] = [610,1200]
    point["regionEvidence"].update(passed=True, marginPx=100)
    point.update(touchTargetErrorPx=70,cameraTouchGapPx=70)
    assert not cb.calibration_region_point_passed(point, SCREEN)


@pytest.mark.parametrize("edit", ["center", "outer_error", "outer_gap", "camera_missing"])
def test_point_rejects_inconsistent_or_missing_evidence(edit):
    point = region_point()
    if edit == "center": point["u"] = .51
    elif edit == "outer_error": point["touchTargetErrorPx"] = 0
    elif edit == "outer_gap": point["cameraTouchGapPx"] = 0
    else: point["regionEvidence"]["cameraPredictedScreen"] = None
    assert not cb.calibration_region_point_passed(point, SCREEN)


@pytest.mark.parametrize("edit", ["no_loo", "summary", "short", "negative", "nan", "large",
                                  "sample_count", "target_enlarged", "sample_center", "training"])
def test_formal_region_rejects_missing_tampered_or_oversized_training_error(edit):
    bundle=region_bundle()
    selection=bundle["xy"]["modelSelection"]
    loo=selection["affine"]["leaveOneOut"]
    if edit == "no_loo": del selection["affine"]["leaveOneOut"]
    elif edit == "summary": loo["maxErrorPx"]=0
    elif edit == "short": loo["residualsPx"].pop()
    elif edit == "negative": loo["residualsPx"][0]=-1
    elif edit == "nan": loo["residualsPx"][0]=float("nan")
    elif edit == "large": loo.update(residualsPx=[100]*9,maxErrorPx=100,rmsPx=100)
    elif edit == "sample_count": selection["sampleCount"]=True
    elif edit == "target_enlarged": bundle["xy"]["samples"][0]["targetRect"]["widthU"] = .5
    elif edit == "sample_center": bundle["xy"]["samples"][0]["u"] = .5
    else: bundle["xy"]["residualRmsPx"]=float("nan")
    assert not cb.automatic_calibration_verified(bundle)
    assert "xy_region_quality_invalid" in _codes(_gate(bundle))


def test_mapping_envelope_includes_real_contact_gap_and_selected_loo():
    bundle=region_bundle()
    assert cb.mapping_error_bound(bundle) == 32
    bundle["camera"]["visualVerify"]={"passed":True,"contactVsNominalMaxPx":0,"contactVsCameraMaxPx":0}
    assert cb.mapping_error_bound(bundle) == 32
    loo=bundle["xy"]["modelSelection"]["affine"]["leaveOneOut"]
    loo.update(residualsPx=[40]*9,maxErrorPx=40,rmsPx=40)
    assert cb.mapping_error_bound(bundle) == 42
    bundle["validation"]["points"][0]["cameraTouchGapPx"]=0
    assert cb.mapping_error_bound(bundle) is None


def test_legacy_pixel_qualification_remains_strict():
    bundle=region_bundle()
    del bundle["validation"]["criterion"]
    bundle["validation"]["tolerancePx"]=10
    assert not cb.automatic_calibration_verified(bundle)
    assert "xy_residual_exceeded" in _codes(_gate(bundle))


@pytest.mark.parametrize("point", [[True,1200],[float("nan"),1200],[float("inf"),1200],[10**400,1200], [540], {"x":540,"y":1200}])
def test_region_rejects_malformed_actual_coordinates(point):
    with pytest.raises(ValueError):
        cb.calibration_region_evidence(TARGET,SCREEN,point)


def test_training_loo_uses_each_corresponding_target_margin():
    bundle=region_bundle()
    small=bundle["xy"]["samples"][0]
    small["targetRect"]["widthU"]=.02
    loo=bundle["xy"]["modelSelection"]["affine"]["leaveOneOut"]
    errors=[8]+[40]*8
    loo.update(residualsPx=errors,maxErrorPx=40,rmsPx=math.hypot(*errors)/3)
    assert cb.automatic_calibration_verified(bundle)
    assert cb.mapping_error_bound(bundle)==42
    errors[0]=10
    loo["rmsPx"]=math.hypot(*errors)/3
    assert not cb.automatic_calibration_verified(bundle)


def test_region_failed_touch_does_not_pass_with_tampered_true_summaries():
    bundle=region_bundle()
    point=bundle["validation"]["points"][0]
    point["regionEvidence"]["actualTouch"]=[610,1200]
    point.update(touchTargetErrorPx=70,cameraTouchGapPx=70)
    assert not cb.automatic_calibration_verified(bundle)
    assert "motion_qualification_incomplete" in _codes(_gate(bundle))
    assert cb.mapping_error_bound(bundle) is None


def test_region_mapping_envelope_recomputes_all_visual_validation_points():
    bundle=region_bundle()
    point=region_point(offset=45)
    point["targetId"]="visual-1"
    bundle["xy"]["validation"]=[point]
    assert cb.mapping_error_bound(bundle)==47
    point["regionEvidence"]["cameraPredictedScreen"]=[500,1200]
    point["cameraTouchGapPx"]=85
    assert cb.mapping_error_bound(bundle)==87
