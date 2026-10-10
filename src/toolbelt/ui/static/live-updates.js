/*
 * Live updates for the `tt ui` page: listens to the server's /events stream
 * (Server-Sent Events) and patches the page in place.
 *
 *   status     {html, v}  one branch's status cells; replaced by element id
 *   structure  {v}        the stack DB changed; re-fetch "/" and swap the rows
 *   refreshed  {v}        a polling pass finished ("updated Ns ago")
 *   hello      {v, age}   on (re)connect; if v is not the last version seen,
 *                         events were missed, so the rows are re-fetched
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

  // The label for the connection indicator.
  function statusLabel(state, updatedSecondsAgo) {
    if (state === "connecting") return "connecting…";
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

    function swapCells(html) {
      const template = document.createElement("template");
      template.innerHTML = html;
      for (const cell of template.content.querySelectorAll("td[id]")) {
        const old = document.getElementById(cell.id);
        if (!old || old.outerHTML === cell.outerHTML) continue;
        old.replaceWith(cell);
        cell.classList.add("flash");
        cell.addEventListener("animationend", () => cell.classList.remove("flash"));
      }
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
        swapCells(data.html);
        updatedAt = Date.now();
      } else if (name === "refreshed") {
        updatedAt = Date.now();
      } else if (name === "structure") {
        return reloadRows();
      }
      return undefined;
    }

    function connect() {
      const source = new window.EventSource("/events");
      const enqueue = (name, task) => {
        chain = chain.then(task).catch(() => {});
      };
      for (const name of ["status", "structure", "refreshed"]) {
        source.addEventListener(name, (event) => {
          enqueue(name, () => handle(name, JSON.parse(event.data)));
        });
      }
      source.addEventListener("hello", (event) => {
        const data = JSON.parse(event.data);
        state = "live";
        updatedAt = data.age === null ? null : Date.now() - data.age * 1000;
        const missed = missedEvents(lastVersion, data.v);
        lastVersion = data.v;
        if (missed) enqueue("hello", reloadRows);
        render();
      });
      source.onerror = () => {
        state = source.readyState === window.EventSource.CLOSED ? "disconnected" : "reconnecting";
        render();
        if (source.readyState === window.EventSource.CLOSED) {
          window.setTimeout(connect, RECONNECT_DELAY_MS);
        }
      };
    }

    window.setInterval(render, 1000);
    render();
    connect();
  }

  return { formatAgo, missedEvents, statusLabel, init };
});
