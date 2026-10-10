/*
 * Live updates for the `tt ui` page: listens to the server's /events stream
 * (Server-Sent Events) and patches the page in place.
 *
 *   status     {html, card, v}  one branch's status cells and the live half
 *                         of its card; each replaced by element id
 *   structure  {v}        the stack DB changed; re-fetch "/" and swap the rows
 *   refreshed  {v}        a polling pass finished ("updated Ns ago")
 *   hello      {v, age}   on (re)connect; if v is not the last version seen,
 *                         events were missed, so the rows are re-fetched
 *
 * The stream is only open while the page is being looked at: once the page
 * has been hidden (background tab, minimized window) for GRACE_MS the stream
 * is closed, which is what makes the server stop polling GitHub, and it is
 * reopened as soon as the page is visible again. The server then refetches
 * whatever went stale meanwhile. A brief tab switch (shorter than the grace
 * period) never closes the stream.
 *
 * After each DOM change a "stack-table:changed" event is dispatched, and
 * stack-table.js re-applies the user's sort and filters, so a live update
 * never resets them (nor the scroll position: only cells and rows change).
 *
 * The first half is pure and unit-tested with `node --test`
 * (tests/js/live-updates.test.mjs).
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.LiveUpdates = api;
    if (typeof document !== "undefined") {
      if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", () => api.init(document, window));
      } else {
        api.init(document, window);
      }
    }
  }
})(typeof self !== "undefined" ? self : this, function () {
  const RECONNECT_DELAY_MS = 3000;
  const GRACE_MS = 15000;
  // How often visibility is re-read in case no event announced a change.
  const VISIBILITY_POLL_MS = 3000;

  // "updated 5s ago" / "updated 3m ago" for a whole number of seconds.
  function formatAgo(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    if (s < 3) return "just now";
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    return `${Math.floor(s / 3600)}h ago`;
  }

  // On connect the server reports the version of the latest event; if it is
  // not the last one this page has seen, something was missed.
  function missedEvents(lastVersion, helloVersion) {
    return lastVersion !== helloVersion;
  }

  // What to do about the stream given the page's visibility: "connect" a
  // missing one when visible, "disconnect" a live one once the page has been
  // hidden for the grace period, "wait" while that period runs.
  function connectionAction({ visible, connected, hiddenForMs, graceMs }) {
    if (visible) return connected ? "none" : "connect";
    if (!connected) return "none";
    return hiddenForMs >= graceMs ? "disconnect" : "wait";
  }

  /*
   * Owns when the stream is open. Everything environmental is injected so the
   * logic is testable without a DOM: isVisible(), now() (ms), open(),
   * close(), setTimer(fn, ms) / clearTimer(handle) and onState(name), which
   * is told "paused" when the stream was closed for being hidden and
   * "resuming" when it is reopened after that.
   */
  function createConnectionController(deps) {
    let connected = false;
    let paused = false;
    let hiddenSince = null;
    let timer = null;

    // A slow poll of isVisible(), for browsers that never dispatch
    // "visibilitychange" (the page would stay paused while shown).
    function schedulePoll() {
      deps.setTimer(() => {
        api.visibilityChanged();
        schedulePoll();
      }, VISIBILITY_POLL_MS);
    }

    function cancelTimer() {
      if (timer !== null) deps.clearTimer(timer);
      timer = null;
    }

    function reconcile() {
      cancelTimer();
      const hiddenForMs = hiddenSince === null ? 0 : deps.now() - hiddenSince;
      const action = connectionAction({
        visible: deps.isVisible(),
        connected,
        hiddenForMs,
        graceMs: GRACE_MS,
      });
      if (action === "connect") {
        connected = true;
        if (paused) deps.onState("resuming");
        paused = false;
        deps.open();
      } else if (action === "disconnect") {
        connected = false;
        paused = true;
        deps.close();
        deps.onState("paused");
      } else if (action === "wait") {
        timer = deps.setTimer(reconcile, GRACE_MS - hiddenForMs);
      }
    }

    const api = {
      // Call once at page load.
      start() {
        schedulePoll();
        if (deps.isVisible()) {
          hiddenSince = null;
        } else {
          // Loaded in a background tab: nothing to poll for until it is shown.
          hiddenSince = deps.now();
          paused = true;
          deps.onState("paused");
        }
        reconcile();
      },
      // Re-reads the page's visibility. Called on "visibilitychange" and also
      // as a fallback (window focus, pageshow, a slow timer): some embedded
      // browsers never dispatch visibilitychange, which used to leave a page
      // that was opened in the background paused for good. Idempotent: the
      // time the page went hidden is kept while it stays hidden, so calling
      // this repeatedly does not restart the grace period.
      visibilityChanged() {
        if (deps.isVisible()) hiddenSince = null;
        else if (hiddenSince === null) hiddenSince = deps.now();
        reconcile();
      },
      // The stream died on its own (not by us); reconnect if still wanted.
      connectionLost() {
        connected = false;
        reconcile();
      },
    };
    return api;
  }

  // The label for the connection indicator.
  function statusLabel(state, updatedSecondsAgo) {
    if (state === "connecting") return "connecting…";
    if (state === "resuming") return "resuming…";
    if (state === "paused") return "paused (tab hidden)";
    if (state === "reconnecting") return "reconnecting…";
    if (state === "disconnected") return "disconnected";
    if (updatedSecondsAgo === null) return "live · updating…";
    return `live · updated ${formatAgo(updatedSecondsAgo)}`;
  }

  // ---- DOM ----------------------------------------------------------------

  function init(document, window) {
    const indicator = document.getElementById("live");
    if (!indicator || typeof window.EventSource === "undefined") return;

    let state = "connecting";
    let lastVersion = Number(indicator.dataset.version);
    let updatedAt = null; // Date.now() of the last refresh we know about
    // Events are handled one at a time, so a status that arrives while the
    // rows are being re-fetched is applied after them, not lost.
    let chain = Promise.resolve();

    function render() {
      indicator.dataset.state = state;
      const ago = updatedAt === null ? null : (Date.now() - updatedAt) / 1000;
      indicator.textContent = statusLabel(state, ago);
    }

    function changed() {
      document.dispatchEvent(new window.CustomEvent("stack-table:changed"));
    }

    function swapById(html, selector) {
      const template = document.createElement("template");
      template.innerHTML = html;
      for (const fresh of template.content.querySelectorAll(selector)) {
        const old = document.getElementById(fresh.id);
        if (!old || old.outerHTML === fresh.outerHTML) continue;
        old.replaceWith(fresh);
        fresh.classList.add("flash");
        fresh.addEventListener("animationend", () => fresh.classList.remove("flash"));
      }
    }

    function swapCells(html, card) {
      swapById(html, "td[id]");
      if (card) swapById(card, ".card-live[id]");
      changed();
    }

    async function reloadRows() {
      const response = await window.fetch(window.location.pathname, {
        headers: { Accept: "text/html" },
      });
      const doc = new window.DOMParser().parseFromString(
        await response.text(),
        "text/html",
      );
      const fresh = doc.querySelector("#stacks tbody");
      const body = document.querySelector("#stacks tbody");
      if (!fresh || !body) {
        // The table appeared or vanished (first/last stack): start over.
        window.location.reload();
        return;
      }
      body.replaceChildren(...[...fresh.children].map((tr) => document.adoptNode(tr)));
      const cards = document.getElementById("cards");
      const freshCards = doc.getElementById("cards");
      if (cards && freshCards) {
        cards.replaceChildren(
          ...[...freshCards.children].map((card) => document.adoptNode(card)),
        );
      }
      for (const id of ["notices", "repo-select"]) {
        const target = document.getElementById(id);
        const source = doc.getElementById(id);
        if (target && source) target.innerHTML = source.innerHTML;
      }
      if (window.htmx) window.htmx.process(body);
      changed();
    }

    function handle(name, data) {
      if (typeof data.v === "number") lastVersion = data.v;
      if (name === "status") {
        swapCells(data.html, data.card);
        updatedAt = Date.now();
      } else if (name === "refreshed") {
        updatedAt = Date.now();
      } else if (name === "structure") {
        return reloadRows();
      }
      return undefined;
    }

    let source = null;
    let reconnectTimer = null;

    function open() {
      const stream = new window.EventSource("/events");
      source = stream;
      const enqueue = (name, task) => {
        chain = chain.then(task).catch(() => {});
      };
      for (const name of ["status", "structure", "refreshed"]) {
        stream.addEventListener(name, (event) => {
          enqueue(name, () => handle(name, JSON.parse(event.data)));
        });
      }
      stream.addEventListener("hello", (event) => {
        const data = JSON.parse(event.data);
        state = "live";
        updatedAt = data.age === null ? null : Date.now() - data.age * 1000;
        const missed = missedEvents(lastVersion, data.v);
        lastVersion = data.v;
        // A page rendered before its statuses were known (opened in a
        // background tab) is also refreshed: whatever arrived meanwhile.
        const loading = document.querySelector(".card-live[data-loading]");
        if (missed || loading) enqueue("hello", reloadRows);
        render();
      });
      stream.onerror = () => {
        if (source !== stream) return;
        if (stream.readyState === window.EventSource.CLOSED) {
          state = "disconnected";
          source = null;
          render();
          window.clearTimeout(reconnectTimer);
          reconnectTimer = window.setTimeout(controller.connectionLost, RECONNECT_DELAY_MS);
        } else {
          state = "reconnecting";
          render();
        }
      };
    }

    function close() {
      window.clearTimeout(reconnectTimer);
      if (source) source.close();
      source = null;
    }

    const controller = createConnectionController({
      isVisible: () => document.visibilityState !== "hidden",
      now: () => Date.now(),
      open,
      close,
      setTimer: (fn, ms) => window.setTimeout(fn, ms),
      clearTimer: (handle) => window.clearTimeout(handle),
      onState: (name) => {
        state = name;
        render();
      },
    });
    document.addEventListener("visibilitychange", controller.visibilityChanged);
    // Fallbacks for environments that do not announce visibility changes.
    window.addEventListener("focus", controller.visibilityChanged);
    window.addEventListener("pageshow", controller.visibilityChanged);

    window.setInterval(render, 1000);
    render();
    controller.start();
  }

  return {
    GRACE_MS,
    VISIBILITY_POLL_MS,
    formatAgo,
    missedEvents,
    connectionAction,
    createConnectionController,
    statusLabel,
    init,
  };
});
