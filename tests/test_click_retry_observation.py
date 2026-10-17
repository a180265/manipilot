"""Click recovery needs the exact measured page and target identity."""
import asyncio

from tests.test_layout_observation import layout_rig, offline_only  # noqa: F401


def test_proof_exposes_live_layout_targets_without_guessing_bounds(layout_rig):
    rig = layout_rig
    rig.source.image = rig.frame("chat_ime_empty")
    proof = asyncio.run(rig.camera.observe_regions())
    assert proof["layout_state"] == "chat_ime_empty"
    frame = asyncio.run(rig.camera._selected_layout_frame())
    assert proof["click_target_frame_size"] == {
        "width": frame.image.shape[1], "height": frame.image.shape[0]}
    expected = {item["id"]: {"status": item["status"],
                            "safe_rect": item.get("safe_rect")}
                for item in frame.check["item_results"] if item["kind"] == "target"}
    assert proof["click_targets"] == expected
    assert proof["adaptation_ref"] == frame.selected.references["adaptation_ref"]
