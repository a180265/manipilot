"""A latest-frame camera may repeat a physical frame before its next exposure."""
from datetime import datetime, timezone

import pytest

from runtime import calibration_gate as gate
from tests.test_live_calibration_gate import FakeCamera, run_gate


class LatestFrameCamera(FakeCamera):
    def __init__(self, sequences, *, override=None):
        super().__init__()
        self.sequences = sequences
        self.override = override or {}
        self.calls = 0

    async def capture(self):
        frame = await super().capture()
        seq = self.sequences[min(self.calls, len(self.sequences) - 1)]
        self.calls += 1
        now = datetime.now(timezone.utc).isoformat()
        frame.update(source_frame_seq=seq, source_captured_at=now,
                     source_received_at=now, source_generation_id="generation-a")
        if self.calls > 1:
            frame.update(self.override)
        return frame


@pytest.fixture(autouse=True)
def isolate_image_and_wait(monkeypatch):
    monkeypatch.setattr(gate, "_frame_checks", lambda *_a, **_kw: ([], None))
    async def no_sleep(_seconds):
        pass
    monkeypatch.setattr(gate.asyncio, "sleep", no_sleep)


def test_consensus_waits_for_four_distinct_physical_frames(monkeypatch):
    camera = LatestFrameCamera([1, 1, 2, 2, 3, 3, 4])
    checks, frame, _ = run_gate(monkeypatch, camera=camera, require_pose=True)
    assert checks == []
    assert frame["source_frame_seq"] == 4
    assert frame["_screen_pose"]["movementPx"] == pytest.approx(0)
    assert camera.calls == 7


def test_nonadvancing_source_still_fails_with_bounded_reads(monkeypatch):
    camera = LatestFrameCamera([1])
    checks, frame, _ = run_gate(monkeypatch, camera=camera, require_pose=True)
    assert "screen_pose_unavailable" in {c["code"] for c in checks}
    assert "_screen_pose" not in frame
    assert camera.calls <= 10


@pytest.mark.parametrize("seq,override", [
    (0, {}),
    (1, {"source_captured_at": "2020-01-01T00:00:00+00:00"}),
    (1, {"source_legacy_mode": True}),
    (1, {"source_generation_id": "generation-b"}),
    (2, {"source_generation_id": "generation-b"}),
    (1, {"device_unique_id": "other-camera"}),
])
def test_invalid_source_is_rejected_without_waiting(monkeypatch, seq, override):
    camera = LatestFrameCamera([1, seq, 2, 3, 4], override=override)
    checks, frame, _ = run_gate(monkeypatch, camera=camera, require_pose=True)
    assert checks
    assert "_screen_pose" not in frame
    assert camera.calls == 2
