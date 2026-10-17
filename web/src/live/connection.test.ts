import { describe, expect, test } from "vitest";
import { formatAgo } from "../logic/format";
import {
  connectionAction,
  createConnectionController,
  missedEvents,
  statusLabel,
  VISIBILITY_POLL_MS,
} from "./connection";

test("formatAgo moves from seconds to minutes to hours", () => {
  expect(formatAgo(0)).toBe("just now");
  expect(formatAgo(2.9)).toBe("just now");
  expect(formatAgo(3)).toBe("3s ago");
  expect(formatAgo(59.9)).toBe("59s ago");
  expect(formatAgo(60)).toBe("1m ago");
  expect(formatAgo(3599)).toBe("59m ago");
  expect(formatAgo(7200)).toBe("2h ago");
  expect(formatAgo(-5)).toBe("just now");
});

test("a reconnect only reloads when the event version moved on", () => {
  expect(missedEvents(7, 7)).toBe(false);
  expect(missedEvents(7, 9)).toBe(true);
  // A restarted server starts counting again.
  expect(missedEvents(40, 0)).toBe(true);
  // Nothing loaded yet: whatever the server says is news.
  expect(missedEvents(null, 0)).toBe(true);
});

test("statusLabel describes the connection state", () => {
  expect(statusLabel("connecting")).toBe("connecting…");
  expect(statusLabel("reconnecting")).toBe("reconnecting…");
  expect(statusLabel("disconnected")).toBe("disconnected");
  expect(statusLabel("live")).toBe("live");
  expect(statusLabel("paused")).toBe("paused (tab hidden)");
  expect(statusLabel("resuming")).toBe("resuming…");
});

test("connectionAction opens the stream only while visible, closes after the grace period", () => {
  const act = (visible: boolean, connected: boolean, hiddenForMs: number) =>
    connectionAction({ visible, connected, hiddenForMs, graceMs: 15000 });
  expect(act(true, false, 0)).toBe("connect");
  expect(act(true, true, 0)).toBe("none");
  expect(act(false, true, 1000)).toBe("wait");
  expect(act(false, true, 15000)).toBe("disconnect");
  expect(act(false, false, 60000)).toBe("none");
});

// A controller whose environment is fake: a clock, a visibility flag and
// timers that fire when the test advances the clock.
function harness({ visible = true } = {}) {
  interface Timer {
    fn: () => void;
    at: number;
    live: boolean;
  }
  const env = {
    visible,
    time: 0,
    opened: 0,
    closed: 0,
    states: [] as string[],
    timers: [] as Timer[],
    advance(ms: number) {
      env.time += ms;
      for (const timer of env.timers) {
        if (timer.live && timer.at <= env.time) {
          timer.live = false;
          timer.fn();
        }
      }
    },
    setVisible(value: boolean) {
      env.visible = value;
      controller.visibilityChanged();
    },
  };
  const controller = createConnectionController({
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
      (timer as Timer).live = false;
    },
    onState: (name) => env.states.push(name),
  });
  return { env, controller };
}

describe("the connection controller", () => {
  test("a visible page opens the stream once", () => {
    const { env, controller } = harness();
    controller.start();
    controller.visibilityChanged();
    expect(env.opened).toBe(1);
    expect(env.states).toEqual([]);
  });

  test("a page hidden past the grace period closes the stream and reopens on return", () => {
    const { env, controller } = harness();
    controller.start();

    env.setVisible(false);
    env.advance(14000);
    expect(env.closed).toBe(0);
    env.advance(1000);
    expect(env.closed).toBe(1);
    expect(env.states).toEqual(["paused"]);

    env.advance(600000);
    env.setVisible(true);
    expect(env.opened).toBe(2);
    expect(env.states).toEqual(["paused", "resuming"]);
  });

  test("a quick tab switch never closes the stream", () => {
    const { env, controller } = harness();
    controller.start();

    env.setVisible(false);
    env.advance(5000);
    env.setVisible(true);
    env.advance(60000); // the grace timer was cancelled
    expect(env.closed).toBe(0);
    expect(env.opened).toBe(1);
  });

  test("a page loaded in a background tab connects only when shown", () => {
    const { env, controller } = harness({ visible: false });
    controller.start();
    env.advance(60000);
    expect(env.opened).toBe(0);
    expect(env.states).toEqual(["paused"]);
    env.setVisible(true);
    expect(env.opened).toBe(1);
    expect(env.states).toEqual(["paused", "resuming"]);
  });

  test("a dropped stream is reopened only while the page is visible", () => {
    const { env, controller } = harness();
    controller.start();
    controller.connectionLost();
    expect(env.opened).toBe(2);

    env.setVisible(false);
    controller.connectionLost();
    expect(env.opened).toBe(2);
    env.setVisible(true);
    expect(env.opened).toBe(3);
  });

  test("loaded hidden, then shown through visibilitychange: connects and resumes", () => {
    const { env, controller } = harness({ visible: false });
    controller.start();
    env.advance(30000);
    expect(env.opened).toBe(0);

    env.setVisible(true);

    expect(env.opened).toBe(1);
    expect(env.states).toEqual(["paused", "resuming"]);
  });

  test("shown with no visibilitychange event: a focus-style re-check connects", () => {
    const { env, controller } = harness({ visible: false });
    controller.start();
    expect(env.opened).toBe(0);

    // The browser never announced it; the page's focus/pageshow handler
    // simply asks the controller to re-read the visibility.
    env.visible = true;
    controller.visibilityChanged();

    expect(env.opened).toBe(1);
    expect(env.states).toEqual(["paused", "resuming"]);
  });

  test("shown with no event at all: the slow timer notices", () => {
    const { env, controller } = harness({ visible: false });
    controller.start();
    env.advance(10000);
    expect(env.opened).toBe(0);

    env.visible = true;
    env.advance(VISIBILITY_POLL_MS);

    expect(env.opened).toBe(1);
    expect(env.states).toEqual(["paused", "resuming"]);
  });

  test("re-checking while hidden does not restart the grace period", () => {
    const { env, controller } = harness();
    controller.start();
    env.setVisible(false);
    env.advance(10000);
    controller.visibilityChanged();
    controller.visibilityChanged();
    env.advance(5000);

    expect(env.closed).toBe(1);
  });
});
