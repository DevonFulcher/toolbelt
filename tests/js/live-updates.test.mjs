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

test("statusLabel says when polling is paused or resuming", () => {
  assert.equal(live.statusLabel("paused", 400), "paused (tab hidden)");
  assert.equal(live.statusLabel("resuming", 400), "resuming…");
});

test("connectionAction opens the stream only while visible, closes after the grace period", () => {
  const act = (visible, connected, hiddenForMs) =>
    live.connectionAction({ visible, connected, hiddenForMs, graceMs: 15000 });
  assert.equal(act(true, false, 0), "connect");
  assert.equal(act(true, true, 0), "none");
  assert.equal(act(false, true, 1000), "wait");
  assert.equal(act(false, true, 15000), "disconnect");
  assert.equal(act(false, false, 60000), "none");
});

// A controller whose environment is fake: a clock, a visibility flag and timers
// that fire when the test advances the clock.
function harness({ visible = true } = {}) {
  const env = { visible, time: 0, opened: 0, closed: 0, states: [], timers: [] };
  const controller = live.createConnectionController({
    isVisible: () => env.visible,
    now: () => env.time,
    open: () => {
      env.opened += 1;
    },
    close: () => {
      env.closed += 1;
    },
    setTimer: (fn, ms) => {
      const timer = { fn, at: env.time + ms, live: true };
      env.timers.push(timer);
      return timer;
    },
    clearTimer: (timer) => {
      timer.live = false;
    },
    onState: (name) => env.states.push(name),
  });
  env.advance = (ms) => {
    env.time += ms;
    for (const timer of env.timers) {
      if (timer.live && timer.at <= env.time) {
        timer.live = false;
        timer.fn();
      }
    }
  };
  env.setVisible = (value) => {
    env.visible = value;
    controller.visibilityChanged();
  };
  env.controller = controller;
  return env;
}

test("a visible page opens the stream once", () => {
  const env = harness();
  env.controller.start();
  env.controller.visibilityChanged();
  assert.equal(env.opened, 1);
  assert.deepEqual(env.states, []);
});

test("a page hidden past the grace period closes the stream and reopens on return", () => {
  const env = harness();
  env.controller.start();

  env.setVisible(false);
  env.advance(14000);
  assert.equal(env.closed, 0);
  env.advance(1000);
  assert.equal(env.closed, 1);
  assert.deepEqual(env.states, ["paused"]);

  env.advance(600000);
  env.setVisible(true);
  assert.equal(env.opened, 2);
  assert.deepEqual(env.states, ["paused", "resuming"]);
});

test("a quick tab switch never closes the stream", () => {
  const env = harness();
  env.controller.start();

  env.setVisible(false);
  env.advance(5000);
  env.setVisible(true);
  env.advance(60000); // the grace timer was cancelled
  assert.equal(env.closed, 0);
  assert.equal(env.opened, 1);
});

test("a page loaded in a background tab connects only when shown", () => {
  const env = harness({ visible: false });
  env.controller.start();
  env.advance(60000);
  assert.equal(env.opened, 0);
  env.setVisible(true);
  assert.equal(env.opened, 1);
  // It was never paused, so it is not "resuming".
  assert.deepEqual(env.states, []);
});

test("a dropped stream is reopened only while the page is visible", () => {
  const env = harness();
  env.controller.start();
  env.controller.connectionLost();
  assert.equal(env.opened, 2);

  env.setVisible(false);
  env.controller.connectionLost();
  assert.equal(env.opened, 2);
  env.setVisible(true);
  assert.equal(env.opened, 3);
});
