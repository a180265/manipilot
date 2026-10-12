import assert from "node:assert/strict";
import test from "node:test";
import {
  bindingDraftDirty, canDeviceAction, createLatestRequest, createPreviewOwner,
  debugConsoleUrl, newBindingDraft, refreshBindingEditor,
} from "../src/pages/device-state.ts";

test("debug links select the device and retain configured URL settings", () => {
  assert.equal(debugConsoleUrl("arm-02"), "http://127.0.0.1:8792/?deviceId=arm-02");
  const url = new URL(debugConsoleUrl("arm a&b", "https://debug.example/tool?mode=read&deviceId=old#gestures")!);
  assert.equal(url.searchParams.get("deviceId"), "arm a&b");
  assert.equal(url.searchParams.get("mode"), "read");
  assert.equal(url.hash, "#gestures");
  assert.equal(debugConsoleUrl("arm-01", "javascript:alert(1)"), null);
  assert.equal(debugConsoleUrl("arm-01", "bad-url"), null);
});

test("device actions use state keys and respect backend transitions", () => {
  assert.equal(canDeviceAction("ready", "pause"), true);
  assert.equal(canDeviceAction("returning_home", "pause"), true);
  assert.equal(canDeviceAction("starting", "pause"), false);
  assert.equal(canDeviceAction("maintenance", "quarantine"), false);
  assert.equal(canDeviceAction("paused", "resume", false), false);
  assert.equal(canDeviceAction("paused", "resume", true), true);
  assert.equal(canDeviceAction("quarantined", "unquarantine"), true);
  assert.equal(canDeviceAction("ready", "unquarantine"), false);
  assert.equal(canDeviceAction("可用", "pause"), false);
  assert.equal(canDeviceAction("unrecognized", "pause"), false);
});

test("newer loads and unmount invalidate older response callbacks", () => {
  const requests = createLatestRequest();
  const first = requests.begin();
  const next = requests.begin();
  assert.equal(first(), false);
  assert.equal(next(), true);
  requests.invalidate();
  assert.equal(next(), false);
  assert.equal(requests.begin()(), true);
});

const editor = (revision = 1) => ({
  revision,
  draft: newBindingDraft({ account: "original" }, { camera: "cam-01" }),
});

test("background refresh preserves edits made while its request was pending", () => {
  const current = editor();
  current.draft.fields.account = "edited";
  const incoming = editor(2);
  assert.equal(refreshBindingEditor(current, incoming), current);
  assert.equal(current.revision, 1);
  assert.equal(current.draft.originalFields.account, "original");
  assert.equal(current.draft.fields.account, "edited");
});

test("resource selections survive refresh; clean or explicitly reset drafts refresh", () => {
  const current = editor();
  current.draft.resources.camera = "cam-02";
  assert.equal(bindingDraftDirty(current.draft), true);
  assert.equal(refreshBindingEditor(current, editor(2)), current);
  const incoming = editor(3);
  assert.equal(refreshBindingEditor(current, incoming, true), incoming);
  assert.equal(refreshBindingEditor(editor(), incoming), incoming);
  current.draft.resources.camera = "";
  assert.equal(bindingDraftDirty(current.draft), false);
});

test("duplicate preview starts are refused before session creation resolves", () => {
  const owner = createPreviewOwner();
  const token = owner.begin()!;
  assert.equal(owner.begin(), null);
  assert.equal(owner.adopt(token, "view-1"), true);
  assert.equal(owner.begin(), null);
  assert.equal(owner.stop(), "view-1");
  assert.equal(owner.stop(), null);
});

test("late creation and frame callbacks cannot replace a newer preview", () => {
  const owner = createPreviewOwner();
  const old = owner.begin()!;
  assert.equal(owner.stop(), null);
  const current = owner.begin()!;
  assert.equal(owner.adopt(old, "late-view"), false);
  assert.equal(owner.current(old), false);
  assert.equal(owner.adopt(current, "current-view"), true);
  assert.equal(owner.stop(), "current-view");
  assert.equal(owner.current(current), false);
});
