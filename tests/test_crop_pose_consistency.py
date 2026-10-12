"""Registration and runtime must match in the same calibrated screen domain."""
from __future__ import annotations

import asyncio
import copy
import hashlib

import cv2
import numpy as np
import pytest

from drivers import screen_proof
from runtime.layout_geometry import LayoutGeometryError, rectify_capture
from runtime.layout_matching import compute_page_check
from tests.test_layout_observation import layout_rig, offline_only  # noqa: F401
from tests.test_screen_proof import png


def rectify(rig, image, *, bundle=None):
    data = png(image)
    binding = {**rig.binding, "source_geometry_id": "b" * 64,
               "source_size": {"width": 1280, "height": 720}}
    evidence = [{
        "device_id": "arm-01", "evidence_id": name,
        "image_sha256": hashlib.sha256(data).hexdigest(),
        "source_binding": {**binding, "device_id": "arm-01", "evidence_id": name},
    } for name in ("pose-source-1", "pose-source-2")]
    return rectify_capture(
        device_id="arm-01", snapshot=binding, current_binding=binding,
        bundle=bundle or rig.bundle, region_record=rig.region,
        capture={"device_id": "arm-01", "evidence": evidence},
        frame_reader=lambda name: data)


@pytest.mark.parametrize("dx,dy", [(0, 0), (7, 0), (-5, 3)])
def test_registration_and_camera_consume_identical_pose_normalized_pixels(layout_rig, dx, dy):
    rig = layout_rig
    state = "chat_ime_empty"
    image = cv2.warpAffine(rig.frame(state), np.float32([[1, 0, dx], [0, 1, dy]]),
                           (1280, 720), borderValue=(20, 20, 20))
    registered = screen_proof.decode(rectify(rig, image)["png"])
    page = next(p for p in rig.template["pages"] if p["state"] == state)
    check = compute_page_check(page, png(registered),
        {a["id"]: rig.assets[a["asset_id"]] for a in page["anchors"]}, [])
    assert check["status"] == "passed", check
    rig.source.image = image
    observed = asyncio.run(rig.camera._selected_layout_frame())
    assert np.array_equal(registered, observed.image)
    assert observed.ui["ui_state"] == "conversation"


@pytest.mark.parametrize("fault", ["missing-edge", "excessive-movement", "unlocated"])
def test_registration_cannot_accept_geometry_runtime_would_reject(layout_rig, fault):
    rig = layout_rig
    bundle = copy.deepcopy(rig.bundle)
    image = rig.frame("chat_ime_empty")
    if fault == "missing-edge":
        bundle["screen"].pop("edgeModel")
    elif fault == "excessive-movement":
        image = cv2.warpAffine(image, np.float32([[1, 0, 22], [0, 1, 0]]),
                               (1280, 720), borderValue=(20, 20, 20))
    else:
        image[:] = 20
    with pytest.raises(LayoutGeometryError):
        rectify(rig, image, bundle=bundle)


def test_second_screen_inside_selected_region_blocks_registration_and_runtime(layout_rig):
    rig = layout_rig
    image = rig.frame("chat_ime_empty")
    image[160:400, 470:600] = 220
    with pytest.raises(LayoutGeometryError, match="multiple_screen_candidates"):
        rectify(rig, image)
    rig.source.image = image
    with pytest.raises(ValueError, match="multiple_screen_candidates"):
        asyncio.run(rig.camera._selected_layout_frame())


def test_second_screen_outside_selected_region_is_not_a_matching_candidate(layout_rig):
    rig = layout_rig
    image = rig.frame("chat_ime_empty")
    image[50:690, 740:1180] = 220
    rig.source.image = image
    registered = screen_proof.decode(rectify(rig, image)["png"])
    observed = asyncio.run(rig.camera._selected_layout_frame())
    assert observed.state == "chat_ime_empty"
    assert np.array_equal(registered, observed.image)
