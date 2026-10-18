"""Prior-location comparison is an explicit exact-text capability option."""
import asyncio

import pytest


@pytest.mark.parametrize("options,accepted", [
    ({"previous_location_id": "original"}, False),
    ({"previous_location_id": "original", "exact": True}, False),
    ({"previous_location_id": "original", "localization_mode": "layout", "exact": True}, False),
    ({"previous_location_id": "original", "localization_mode": "screen_text"}, False),
    ({"previous_location_id": "original", "localization_mode": "screen_text", "exact": False}, False),
    ({"previous_location_id": "", "localization_mode": "screen_text", "exact": True}, False),
    ({"previous_location_id": 42, "localization_mode": "screen_text", "exact": True}, False),
    ({"previous_location_id": "original", "localization_mode": "screen_text", "exact": True}, True),
])
def test_prior_location_schema_requires_explicit_exact_screen_text(options, accepted):
    from jsonschema import Draft202012Validator
    from runtime.capabilities import BASE_CAPABILITIES

    schema = next(item["input_schema"] for item in BASE_CAPABILITIES
                  if item["capability_id"] == "camera.locate_text")
    assert Draft202012Validator(schema).is_valid({"text": "Ann", **options}) is accepted


def test_runtime_forwards_anchor_and_original_query_to_camera(tmp_path, monkeypatch):
    from tasks.plugin_api import StepContext
    from tests.helpers import PROBE_MANIFEST, make_device_configs, make_runtime

    async def scenario():
        manifest = {**PROBE_MANIFEST,
                    "capabilities": [*PROBE_MANIFEST["capabilities"], "camera.locate_text"]}
        runtime = make_runtime(tmp_path, manifest=manifest)
        await runtime.startup(device_configs=make_device_configs())
        try:
            task = {"task_id": "text-relocation", "device_id": "arm-01",
                    "plugin_id": manifest["plugin_id"], "status": "executing",
                    "lease_id": runtime.leases.acquire("arm-01", "text-relocation")["lease_id"]}
            runtime.store.save_task(task)
            calls = []

            async def locate(text, **kwargs):
                calls.append((text, kwargs))
                return {"found": False}

            monkeypatch.setattr(runtime.devices.camera("arm-01"), "locate_text", locate)
            params = {"text": "Ann", "region": "account", "min_confidence": .75,
                      "exact": True, "localization_mode": "screen_text",
                      "previous_location_id": "original",
                      "roi": {"x1": 100, "y1": 200, "x2": 300, "y2": 400,
                              "frameW": 500, "frameH": 700}}
            result = await StepContext(runtime, task, manifest).call_capability("camera.locate_text", params)
            assert result == {"found": False}
            assert calls == [("Ann", {key: value for key, value in params.items() if key != "text"})]
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())
