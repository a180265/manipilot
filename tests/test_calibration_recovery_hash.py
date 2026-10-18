"""A newly promoted calibration must survive the first serial reconnect."""
from copy import deepcopy

import pytest

from drivers import calibration_bundle as cb
from drivers.calibration_auto_runner import AUTO_PROMOTE_GUARD_KEYS
from runtime import automatic_gestures, recovery
from tests.test_calibration_z_provenance import env, no_network, promote  # noqa: F401


@pytest.mark.parametrize('deferred', [True, False], ids=['motion-ready', 'ready'])
def test_promoted_geometry_can_be_recovered_without_rewriting_its_digest(
        env, tmp_path, monkeypatch, deferred):
    promote(env, deferred=deferred)
    bundle = cb.load_bundle('arm-01', tmp_path)
    keys = tuple(k for k in AUTO_PROMOTE_GUARD_KEYS
                 if k not in ('daemonSession', 'status'))
    assert bundle['recoveryGeometryHash'] == cb.profile_hash(bundle, keys=keys)
    calls = []
    monkeypatch.setattr(automatic_gestures, 'restore_certificates',
                        lambda *a, **kw: {'version': 1})
    result = recovery.attempt_recovery(
        'arm-01', bundle=bundle, new_session='reconnected', data_root=tmp_path,
        arm_anchor_verifier=lambda device: calls.append(device) or True)
    assert result['recovered'] is True, result
    assert calls == ['arm-01']
    restored = cb.load_bundle('arm-01', tmp_path)
    assert restored['daemonSession'] == 'reconnected'
    assert restored['xy'] == bundle['xy']
    changed = deepcopy(restored)
    changed['xy']['robotToScreen']['c'] += 1
    calls.clear()
    result = recovery.attempt_recovery(
        'arm-01', bundle=changed, new_session='another-session', data_root=tmp_path,
        arm_anchor_verifier=lambda device: calls.append(device) or True)
    assert result['stage'] == 'geometry_changed'
    assert calls == []
