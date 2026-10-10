// Unit tests for the pure half of src/toolbelt/ui/static/live-updates.js.
// Run with `node --test tests/js` (tests/test_ui_script.py does so from pytest).
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const live = require("../../src/toolbelt/ui/static/live-updates.js");

test("formatAgo moves from seconds to minutes to hours", () => {
  assert.equal(live.formatAgo(0), "just now");
  assert.equal(live.formatAgo(2.9), "just now");
  assert.equal(live.formatAgo(3), "3s ago");
  assert.equal(live.formatAgo(59.9), "59s ago");
  assert.equal(live.formatAgo(60), "1m ago");
  assert.equal(live.formatAgo(3599), "59m ago");
  assert.equal(live.formatAgo(7200), "2h ago");
  assert.equal(live.formatAgo(-5), "just now");
});

test("a reconnect only reloads when the event version moved on", () => {
  assert.equal(live.missedEvents(7, 7), false);
  assert.equal(live.missedEvents(7, 9), true);
  // A restarted server starts counting again.
  assert.equal(live.missedEvents(40, 0), true);
});

test("statusLabel describes the connection state", () => {
  assert.equal(live.statusLabel("connecting", null), "connecting…");
  assert.equal(live.statusLabel("reconnecting", 4), "reconnecting…");
  assert.equal(live.statusLabel("disconnected", 4), "disconnected");
  assert.equal(live.statusLabel("live", null), "live · updating…");
  assert.equal(live.statusLabel("live", 12), "live · updated 12s ago");
});
