/*
 * Sorting, filtering and the views of the `tt ui` page: the stack table (tree
 * or flat), a grid of cards and an event timeline.
 *
 * The table is the source of truth: its cells carry the sort/filter values,
 * and the cards and timeline are laid out from the same rows (a card is
 * matched to its row by id; its live half carries the branch's events).
 *
 * Two layers. The first half is pure (no DOM): it parses/serialises the view
 * state kept in the URL query string and computes which rows show, in what
 * order and at what depth. It is unit-tested with `node --test`
 * (tests/js/stack-table.test.mjs). The second half reads the table's cells
 * (data-value / data-sort / data-search attributes written by the server),
 * applies that layout to the DOM, and re-applies it whenever the page is updated
 * live (static/live-updates.js dispatches "stack-table:changed" after swapping
 * cells or rows), keeping the user's sort, filters and scroll position.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.StackTable = api;
    if (typeof document !== "undefined") {
      if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", () => api.init(document, window));
      } else {
        api.init(document, window);
      }
    }
  }
})(typeof self !== "undefined" ? self : this, function () {
  const COLUMNS = [
    "repo",
    "branch",
    "next",
    "pr",
    "ci",
    "review",
    "comments",
    "merge",
    "task",
    "session",
  ];
  // Columns with an exact-match value filter (the repo filter is separate:
  // it scopes the whole table rather than hiding rows).
  const VALUE_FILTERS = ["next", "pr", "ci", "review", "comments", "merge"];
  const VIEWS = ["tree", "flat", "cards", "timeline"];
  // The timeline shows at most this many of the most recent events.
  const TIMELINE_LIMIT = 200;
  const DEFAULT_STATE = {
    view: "tree",
    sort: "",
    dir: "asc",
    kind: "",
    order: "newest",
    q: "",
    repo: "",
    pr: "",
    ci: "",
    review: "",
    comments: "",
    merge: "",
    next: "",
  };

  // ---- state <-> query string -------------------------------------------

  function parseState(search) {
    const params = new URLSearchParams(search);
    const state = { ...DEFAULT_STATE };
    const view = params.get("view");
    if (VIEWS.includes(view)) state.view = view;
    state.kind = params.get("kind") || "";
    if (params.get("order") === "oldest") state.order = "oldest";
    const sort = params.get("sort");
    if (COLUMNS.includes(sort)) state.sort = sort;
    if (params.get("dir") === "desc") state.dir = "desc";
    for (const key of ["q", "repo", ...VALUE_FILTERS]) {
      state[key] = params.get(key) || "";
    }
    return state;
  }

  // Only non-default values are written, so the default view is a bare URL.
  function serializeState(state) {
    const params = new URLSearchParams();
    if (state.view !== DEFAULT_STATE.view) params.set("view", state.view);
    if (state.sort) {
      params.set("sort", state.sort);
      if (state.dir !== DEFAULT_STATE.dir) params.set("dir", state.dir);
    }
    if (state.kind) params.set("kind", state.kind);
    if (state.order !== DEFAULT_STATE.order) params.set("order", state.order);
    for (const key of ["q", "repo", ...VALUE_FILTERS]) {
      if (state[key]) params.set(key, state[key]);
    }
    const text = params.toString();
    return text ? "?" + text : "";
  }

  // Clicking the sorted column flips its direction; a new column starts asc.
  function toggleSort(state, column) {
    if (state.sort === column) {
      return { ...state, dir: state.dir === "asc" ? "desc" : "asc" };
    }
    return { ...state, sort: column, dir: "asc" };
  }

  function filtersActive(state) {
    return Boolean(state.q.trim()) || VALUE_FILTERS.some((key) => state[key]);
  }

  // ---- ordering -----------------------------------------------------------

  const NUMBER = /^-?\d+(\.\d+)?$/;

  // Compare two sort keys ascending: numerically when both are numbers, else
  // as natural-order text ("ABC-9" before "ABC-10").
  function compareKeys(a, b) {
    if (NUMBER.test(a) && NUMBER.test(b)) return Number(a) - Number(b);
    return a.localeCompare(b, "en", { numeric: true, sensitivity: "base" });
  }

  // Rows with no value for the sort column always go last, in both
  // directions; ties keep the server's order.
  function rowComparator(state) {
    const sign = state.dir === "desc" ? -1 : 1;
    return (a, b) => {
      if (state.sort) {
        const ka = a.cells[state.sort] ? a.cells[state.sort].sort : "";
        const kb = b.cells[state.sort] ? b.cells[state.sort].sort : "";
        if (ka === "" && kb !== "") return 1;
        if (kb === "" && ka !== "") return -1;
        if (ka !== "" && kb !== "") {
          const result = compareKeys(ka, kb);
          if (result !== 0) return sign * result;
        }
      }
      return a.order - b.order;
    };
  }

  // ---- filtering ----------------------------------------------------------

  // Whether a row is in the repo chosen in the repo filter (all, if none).
  function inScope(row, state) {
    return !state.repo || row.repo === state.repo;
  }

  // Whether a tracked row passes the repo, text and per-column filters. Every
  // whitespace-separated term of the text filter must appear.
  function matches(row, state) {
    if (!row.tracked || !inScope(row, state)) return false;
    for (const key of VALUE_FILTERS) {
      if (state[key] && (row.cells[key] ? row.cells[key].value : "") !== state[key]) {
        return false;
      }
    }
    const haystack = row.search.toLowerCase();
    return state.q
      .toLowerCase()
      .split(/\s+/)
      .filter(Boolean)
      .every((term) => haystack.includes(term.replace(/^#/, "")));
  }

  /*
   * The rows to show, in order, as [{id, depth}].
   *
   * rows: [{id, repo, parent, order, tracked, search, cells: {col: {value,
   * sort}}}] for the whole table (ids are unique across repos). Flat view:
   * the matching tracked rows, sorted. Tree view: the same rows plus their
   * ancestors (kept for context), depth-first, with the sort applied among
   * siblings; each repo's rows stay together, and the repos themselves are in
   * server (name) order, or by the Repo column when it is the sort. With no
   * filter every row of the chosen repos (untracked bases included) shows.
   */
  function layout(rows, state) {
    // Cards are a flat, sorted list; by default the rows that need the user
    // come first. The timeline only needs the matching rows.
    if (state.view === "cards") {
      state = { ...state, view: "flat", sort: state.sort || "next" };
    } else if (state.view === "timeline") {
      state = { ...state, view: "flat" };
    }
    const compare = rowComparator(state);
    if (state.view === "flat") {
      return rows
        .filter((row) => matches(row, state))
        .sort(compare)
        .map((row) => ({ id: row.id, depth: 0 }));
    }

    const byId = new Map(rows.map((row) => [row.id, row]));
    let keep;
    if (filtersActive(state)) {
      keep = new Set();
      for (const row of rows) {
        if (!matches(row, state)) continue;
        for (let r = row; r && !keep.has(r.id); r = byId.get(r.parent)) {
          keep.add(r.id);
        }
      }
    } else {
      keep = new Set(rows.filter((row) => inScope(row, state)).map((row) => row.id));
    }

    const children = new Map();
    const rootsByRepo = new Map();
    for (const row of rows) {
      if (!keep.has(row.id)) continue;
      if (row.parent && row.parent !== row.id && byId.has(row.parent)) {
        if (!children.has(row.parent)) children.set(row.parent, []);
        children.get(row.parent).push(row);
      } else {
        if (!rootsByRepo.has(row.repo)) rootsByRepo.set(row.repo, []);
        rootsByRepo.get(row.repo).push(row);
      }
    }

    // Repos in the server's order (the first row of each), or by the Repo
    // column when that is the sort.
    const repoGroups = [...rootsByRepo.values()].sort((a, b) => {
      if (state.sort === "repo") return compare(a[0], b[0]);
      return a[0].order - b[0].order;
    });

    const out = [];
    const visit = (row, depth) => {
      out.push({ id: row.id, depth });
      for (const child of (children.get(row.id) || []).sort(compare)) {
        visit(child, depth + 1);
      }
    };
    for (const group of repoGroups) {
      for (const row of group.sort(compare)) visit(row, 0);
    }
    return out;
  }

  // ---- timeline -------------------------------------------------------------

  /*
   * The event feed: every event of every matching branch (rows carry
   * ``events`` [{t, kind, actor, detail}], ``branch``, ``repoName``,
   * ``number``, ``url``), newest first unless state.order is "oldest", cut to
   * the most recent ``limit``. Returns {entries, total}.
   */
  function timeline(rows, state, limit = TIMELINE_LIMIT) {
    const all = [];
    for (const row of rows) {
      if (!matches(row, state)) continue;
      for (const event of row.events || []) {
        if (state.kind && event.kind !== state.kind) continue;
        all.push({
          id: row.id,
          branch: row.branch,
          repoName: row.repoName,
          number: row.number,
          url: row.url,
          t: event.t,
          kind: event.kind,
          actor: event.actor,
          detail: event.detail,
          ms: Date.parse(event.t),
          seq: all.length,
        });
      }
    }
    // Newest first, ties in reverse source order (so that, flipped, events
    // that share a time read in the order they happened).
    all.sort((a, b) => b.ms - a.ms || b.seq - a.seq);
    const entries = all.slice(0, limit);
    if (state.order === "oldest") entries.reverse();
    return { entries, total: all.length };
  }

  // ---- ages ---------------------------------------------------------------

  // "<1m", "5m", "3h", "4d", "3w": kept in step with pr_activity.format_age.
  function formatAge(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    if (s < 60) return "<1m";
    if (s < 3600) return `${Math.floor(s / 60)}m`;
    if (s < 86400) return `${Math.floor(s / 3600)}h`;
    if (s < 14 * 86400) return `${Math.floor(s / 86400)}d`;
    return `${Math.floor(s / (7 * 86400))}w`;
  }

  const EVENT_LABELS = {
    opened: ["＋", "opened"],
    ready_for_review: ["▶", "ready for review"],
    converted_to_draft: ["◐", "back to draft"],
    review_requested: ["→", "review requested"],
    review_request_removed: ["⊘", "request removed"],
    review: ["✎", "review"],
    commit: ["•", "commit"],
    force_push: ["↯", "force push"],
    merged: ["⇢", "merged"],
    closed: ["✕", "closed"],
    reopened: ["↺", "reopened"],
  };

  // ---- DOM ----------------------------------------------------------------

  function readRow(tr) {
    const cells = {};
    const search = [];
    for (const td of tr.querySelectorAll("td[data-col]")) {
      cells[td.dataset.col] = {
        value: td.dataset.value || "",
        sort: td.dataset.sort || "",
        search: td.dataset.search || "",
      };
      if (td.dataset.search) search.push(td.dataset.search);
    }
    return {
      id: tr.dataset.id,
      repo: tr.dataset.repo,
      branch: tr.dataset.branch,
      repoName: cells.repo ? cells.repo.sort : "",
      number: cells.pr ? cells.pr.search : "",
      parent: tr.dataset.parent || null,
      order: Number(tr.dataset.order),
      tracked: tr.dataset.tracked === "1",
      search: search.join(" "),
      cells,
    };
  }

  function init(document, window) {
    let state = parseState(window.location.search);
    const controls = document.getElementById("controls");
    const table = document.getElementById("stacks");
    const tableWrap = document.getElementById("table-wrap");
    const cardsEl = document.getElementById("cards");
    const timelineEl = document.getElementById("timeline");

    function syncControls() {
      if (!controls) return;
      for (const key of ["q", "repo", ...VALUE_FILTERS]) {
        const field = controls.elements[key];
        if (field) field.value = state[key];
      }
      for (const button of controls.querySelectorAll("[data-view]")) {
        button.setAttribute("aria-pressed", String(button.dataset.view === state.view));
      }
      for (const key of ["sort", "kind"]) {
        if (controls.elements[key]) controls.elements[key].value = state[key];
      }
      // Controls that only mean something in one view.
      for (const el of controls.querySelectorAll("[data-views]")) {
        el.hidden = el.dataset.views !== state.view;
      }
      const order = controls.querySelector("#order");
      if (order) order.textContent = state.order === "oldest" ? "Oldest first" : "Newest first";
    }

    // Puts ``items`` (elements keyed by row id) into ``container`` in the
    // order of ``shown``, hiding the rest.
    function arrange(container, items, shown) {
      const shownIds = new Set(shown.map((entry) => entry.id));
      const desired = shown.map((entry) => items.get(entry.id)).filter(Boolean);
      for (const [id, el] of items) {
        el.hidden = !shownIds.has(id);
        if (!shownIds.has(id)) desired.push(el);
      }
      if (desired.some((el, index) => el !== container.children[index])) {
        for (const el of desired) container.appendChild(el);
      }
    }

    // A card's task slot shows what the table's task cell shows (the title
    // and status arrive there by htmx), so each task is looked up once.
    function syncCardTasks(cards, byId) {
      for (const [id, card] of cards) {
        const td = byId.get(id) && byId.get(id).querySelector("td.col-task");
        const slot = card.querySelector(".card-task");
        if (td && slot && slot.innerHTML !== td.innerHTML) slot.innerHTML = td.innerHTML;
      }
    }

    function renderTimeline(rows, cards) {
      for (const row of rows) {
        const live = cards.get(row.id) && cards.get(row.id).querySelector(".card-live");
        row.url = live ? live.dataset.url : "";
        try {
          row.events = live ? JSON.parse(live.dataset.events || "[]") : [];
        } catch (error) {
          row.events = [];
        }
      }
      const { entries, total } = timeline(rows, state);
      const list = document.getElementById("timeline-list");
      const items = entries.map((entry) => {
        const [icon, label] = EVENT_LABELS[entry.kind] || ["·", entry.kind];
        const li = document.createElement("li");
        li.className = `ev ev-${entry.kind}`;
        const time = document.createElement("time");
        time.className = "ev-time";
        time.dataset.since = entry.t;
        time.dataset.suffix = " ago";
        time.title = new Date(entry.t).toLocaleString();
        const kind = document.createElement("span");
        kind.className = "ev-kind";
        const glyph = document.createElement("span");
        glyph.className = "ev-icon";
        glyph.textContent = icon;
        kind.append(glyph, label);
        const what = document.createElement("span");
        what.className = "ev-what";
        const repo = document.createElement("span");
        repo.className = "repo-chip";
        repo.textContent = entry.repoName;
        const target = document.createElement(entry.url ? "a" : "span");
        target.textContent = entry.number ? `${entry.branch} #${entry.number}` : entry.branch;
        if (entry.url) {
          target.href = entry.url;
          target.target = "_blank";
          target.rel = "noopener";
        }
        const actor = document.createElement("span");
        actor.className = "ev-actor";
        actor.textContent = entry.actor;
        const detail = document.createElement("span");
        detail.className = "ev-detail";
        detail.textContent = entry.detail;
        what.append(repo, target, actor, detail);
        li.append(time, kind, what);
        return li;
      });
      if (total > entries.length) {
        const more = document.createElement("li");
        more.className = "ev-more";
        more.textContent = `Showing the ${entries.length} most recent of ${total} events.`;
        items.push(more);
      }
      list.replaceChildren(...items);
      refreshAges();
      return entries.length;
    }

    // Re-writes every [data-since] element's text from the current time, so
    // ages stay right between server updates.
    function refreshAges() {
      const now = Date.now();
      for (const el of document.querySelectorAll("[data-since]")) {
        const since = Date.parse(el.dataset.since);
        if (Number.isNaN(since)) continue;
        const text = formatAge((now - since) / 1000) + (el.dataset.suffix || "");
        if (el.textContent !== text) el.textContent = text;
      }
    }

    function apply() {
      if (!table) return;
      const tbody = table.querySelector("tbody");
      const trs = [...tbody.querySelectorAll("tr")];
      const byId = new Map(trs.map((tr) => [tr.dataset.id, tr]));
      const rows = trs.map(readRow);
      const showTable = state.view === "tree" || state.view === "flat";
      if (tableWrap) tableWrap.hidden = !showTable;
      if (cardsEl) cardsEl.hidden = state.view !== "cards";
      if (timelineEl) timelineEl.hidden = state.view !== "timeline";
      const cards = new Map(
        cardsEl ? [...cardsEl.children].map((card) => [card.dataset.id, card]) : [],
      );
      syncCardTasks(cards, byId);
      const shown = layout(rows, state);
      const shownIds = new Set(shown.map((entry) => entry.id));
      if (state.view === "cards") {
        arrange(cardsEl, cards, shown);
      } else if (state.view === "timeline") {
        renderTimeline(rows, cards);
      }

      const desired = shown.map((entry) => byId.get(entry.id));
      for (const tr of trs) if (!shownIds.has(tr.dataset.id)) desired.push(tr);
      shown.forEach((entry) => {
        const tr = byId.get(entry.id);
        tr.hidden = false;
        tr.style.setProperty("--depth", String(entry.depth));
      });
      for (const tr of trs) if (!shownIds.has(tr.dataset.id)) tr.hidden = true;
      if (desired.some((tr, index) => tr !== tbody.children[index])) {
        for (const tr of desired) tbody.appendChild(tr);
      }

      for (const th of table.querySelectorAll("thead th[data-col]")) {
        const sorted = th.dataset.col === state.sort;
        th.setAttribute(
          "aria-sort",
          sorted ? (state.dir === "desc" ? "descending" : "ascending") : "none",
        );
      }
      // Counts are of matching branches; tree view also shows non-matching
      // ancestors for context.
      const inRepo = rows.filter((row) => row.tracked && inScope(row, state));
      const visible = rows.filter((row) => matches(row, state)).length;
      const count = document.getElementById("count");
      if (count) count.textContent = `${visible} of ${inRepo.length} branches`;
      const none = document.getElementById("no-match");
      if (none) none.hidden = visible > 0 || inRepo.length === 0;
      refreshAges();
    }

    function commit(next) {
      state = next;
      const query = serializeState(state);
      window.history.replaceState(null, "", window.location.pathname + query);
      syncControls();
      apply();
    }

    if (controls) {
      const read = () => {
        const next = { ...state };
        for (const key of ["q", "repo", "kind", ...VALUE_FILTERS]) {
          next[key] = controls.elements[key].value;
        }
        commit(next);
      };
      controls.addEventListener("input", read);
      controls.addEventListener("change", (event) => {
        if (event.target.name === "sort") {
          commit({ ...state, sort: event.target.value, dir: "asc" });
        } else {
          read();
        }
      });
      controls.addEventListener("click", (event) => {
        const button = event.target.closest("button");
        if (!button) return;
        if (button.dataset.view) commit({ ...state, view: button.dataset.view });
        if (button.id === "order") {
          commit({ ...state, order: state.order === "oldest" ? "newest" : "oldest" });
        }
        if (button.id === "clear") commit({ ...DEFAULT_STATE, view: state.view });
      });
    }
    document.addEventListener("click", (event) => {
      const th = event.target.closest("thead th[data-col]");
      if (th) commit(toggleSort(state, th.dataset.col));
    });

    // Task info swaps in asynchronously via htmx, and live updates swap in
    // cells or rows ("stack-table:changed"): re-apply afterwards. A burst of
    // swaps (one per branch) is coalesced into one pass.
    let pending = false;
    const reapply = () => {
      if (pending) return;
      pending = true;
      window.setTimeout(() => {
        pending = false;
        syncControls();
        apply();
      }, 0);
    };
    document.body.addEventListener("htmx:afterSettle", reapply);
    document.addEventListener("stack-table:changed", reapply);

    window.setInterval(refreshAges, 30000);

    syncControls();
    apply();
  }

  return {
    VIEWS,
    TIMELINE_LIMIT,
    formatAge,
    timeline,
    COLUMNS,
    VALUE_FILTERS,
    DEFAULT_STATE,
    parseState,
    serializeState,
    toggleSort,
    filtersActive,
    compareKeys,
    matches,
    layout,
    init,
  };
});
