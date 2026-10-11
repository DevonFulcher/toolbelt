import { describe, expect, test } from "vitest";
import { createAppStore } from "./appStore";
import { localPreferences } from "./localPreferences";
import { syncUrl } from "./urlSync";
import { DEFAULT_STATE } from "../logic/viewState";
import { branch, noPr, pr, snapshot } from "../testing/fixtures";
import { fakeUrl } from "../testing/render";

describe("applySnapshot", () => {
  test("splits branches into structure and live status, in server order", () => {
    const store = createAppStore();

    store.getState().applySnapshot(
      snapshot([
        branch(1, "a", { status: pr() }),
        branch(1, "b", { parent: "a", task_fetchable: true, task_live: { state: "loading" } }),
      ]),
      1000,
    );

    const state = store.getState();
    expect(state.ready).toBe(true);
    expect(state.branches.map((b) => b.id)).toEqual(["1:a", "1:b"]);
    expect(state.statuses["1:a"].load).toBe("loaded");
    expect(state.statuses["1:b"]).toEqual({ load: "loading" });
    expect(state.tasks).toEqual({ "1:b": { state: "loading" } });
    expect(state.reposById[1].name).toBe("api");
    expect("status" in state.branches[0]).toBe(false);
  });

  test("keeps the objects of branches that did not change (so only changed cards re-render)", () => {
    const store = createAppStore();
    store.getState().applySnapshot(snapshot([branch(1, "a", { status: pr() }), branch(1, "b", { status: noPr() })]));
    const before = store.getState();

    store.getState().applySnapshot(
      snapshot([branch(1, "a", { status: pr() }), branch(1, "b", { status: pr({ ci: "failed" }) })], undefined, {
        version: 3,
      }),
    );

    const after = store.getState();
    expect(after.statuses["1:a"]).toBe(before.statuses["1:a"]);
    expect(after.statuses["1:b"]).not.toBe(before.statuses["1:b"]);
    expect(after.branchesById["1:a"]).toBe(before.branchesById["1:a"]);
    expect(after.catalog).toBe(before.catalog);
    expect(after.version).toBe(3);
  });

  test("dates the last refresh from the snapshot's age", () => {
    const store = createAppStore();

    store.getState().applySnapshot(snapshot([], undefined, { age: 12 }), 100_000);

    expect(store.getState().updatedAt).toBe(88_000);
  });
});

describe("patches", () => {
  const loaded = () => {
    const store = createAppStore({ clock: () => 5000 });
    store.getState().applySnapshot(snapshot([branch(1, "a", { status: pr() })], undefined, { version: 4 }));
    return store;
  };

  test("a status patch replaces that branch's status only", () => {
    const store = loaded();
    const other = store.getState().branchesById;

    store.getState().statusChanged("1:a", pr({ ci: "failed" }), 5);

    expect(store.getState().statuses["1:a"]).toMatchObject({ ci: "failed" });
    expect(store.getState().branchesById).toBe(other);
    expect(store.getState().version).toBe(5);
    expect(store.getState().updatedAt).toBe(5000);
  });

  test("an identical status is not a change", () => {
    const store = loaded();
    const before = store.getState().statuses;

    store.getState().statusChanged("1:a", pr(), 5);

    expect(store.getState().statuses).toBe(before);
  });

  test("events at or below the snapshot's version are already reflected and ignored", () => {
    const store = loaded();

    store.getState().statusChanged("1:a", pr({ ci: "failed" }), 4);
    store.getState().taskChanged("1:a", { state: "info", title: "T", status: "S" }, 3);

    expect(store.getState().statuses["1:a"]).toMatchObject({ ci: "success" });
    expect(store.getState().tasks).toEqual({});
  });
});

describe("preferences", () => {
  const memory = (): Storage => {
    const data = new Map<string, string>();
    return {
      getItem: (k) => data.get(k) ?? null,
      setItem: (k, v) => void data.set(k, v),
      removeItem: (k) => void data.delete(k),
      clear: () => data.clear(),
      key: () => null,
      length: 0,
    };
  };

  test("remember the compact choice and the collapsed sections", () => {
    const storage = memory();
    const prefs = localPreferences(storage);
    const store = createAppStore({ preferences: prefs });

    store.getState().setView({ ...DEFAULT_STATE, compact: "1" });
    store.getState().toggleSection("ci");

    expect(storage.getItem("tt-ui-compact")).toBe("1");
    expect(localPreferences(storage).loadCollapsed()).toEqual(["ci"]);
    expect(createAppStore({ preferences: localPreferences(storage) }).getState().collapsed).toEqual(["ci"]);
  });

  test("remember which cards have their log open, by card id", () => {
    const storage = memory();
    const store = createAppStore({ preferences: localPreferences(storage) });

    store.getState().toggleLog("1:devon/a");
    store.getState().toggleLog("1:devon/b");
    store.getState().toggleLog("1:devon/a");

    expect(store.getState().logOpen).toEqual(["1:devon/b"]);
    expect(JSON.parse(storage.getItem("tt-ui-log-open") as string)).toEqual(["1:devon/b"]);
    expect(createAppStore({ preferences: localPreferences(storage) }).getState().logOpen).toEqual([
      "1:devon/b",
    ]);
  });

  test("a blocked or garbled store is just not remembered", () => {
    const blocked = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    } as unknown as Storage;
    const prefs = localPreferences(blocked);

    expect(prefs.loadCompact()).toBeNull();
    expect(prefs.loadCollapsed()).toEqual([]);
    expect(prefs.loadLogOpen()).toEqual([]);
    expect(() => prefs.saveCompact("1")).not.toThrow();
    expect(() => prefs.saveLogOpen(["1:devon/a"])).not.toThrow();

    const garbled = memory();
    garbled.setItem("tt-ui-collapsed", "{not json");
    expect(localPreferences(garbled).loadCollapsed()).toEqual([]);
    expect(localPreferences(null).loadCompact()).toBeNull();
  });
});

describe("syncUrl", () => {
  test("writes the view state to the address bar as it changes, with replaceState", () => {
    const store = createAppStore();
    const url = fakeUrl("");
    const stop = syncUrl(store, url);

    store.getState().setView({ ...DEFAULT_STATE, view: "stacked", q: "auth" });
    expect(url.location.search).toBe("?view=stacked&q=auth");

    stop();
    store.getState().setView({ ...DEFAULT_STATE });
    expect(url.location.search).toBe("?view=stacked&q=auth");
  });
});
