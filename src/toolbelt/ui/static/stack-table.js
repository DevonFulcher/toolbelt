/*
 * Sorting, filtering and tree/flat layout for the `tt ui` stack table.
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
  const DEFAULT_STATE = {
    view: "tree",
    sort: "",
    dir: "asc",
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
    if (params.get("view") === "flat") state.view = "flat";
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

  // ---- DOM ----------------------------------------------------------------

  function readRow(tr) {
    const cells = {};
    const search = [];
    for (const td of tr.querySelectorAll("td[data-col]")) {
      cells[td.dataset.col] = {
        value: td.dataset.value || "",
        sort: td.dataset.sort || "",
      };
      if (td.dataset.search) search.push(td.dataset.search);
    }
    return {
      id: tr.dataset.id,
      repo: tr.dataset.repo,
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

    function syncControls() {
      if (!controls) return;
      for (const key of ["q", "repo", ...VALUE_FILTERS]) {
        const field = controls.elements[key];
        if (field) field.value = state[key];
      }
      for (const button of controls.querySelectorAll("[data-view]")) {
        button.setAttribute("aria-pressed", String(button.dataset.view === state.view));
      }
    }

    function apply() {
      if (!table) return;
      const tbody = table.querySelector("tbody");
      const trs = [...tbody.querySelectorAll("tr")];
      const byId = new Map(trs.map((tr) => [tr.dataset.id, tr]));
      const rows = trs.map(readRow);
      const shown = layout(rows, state);
      const shownIds = new Set(shown.map((entry) => entry.id));

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
        for (const key of ["q", "repo", ...VALUE_FILTERS]) {
          next[key] = controls.elements[key].value;
        }
        commit(next);
      };
      controls.addEventListener("input", read);
      controls.addEventListener("change", read);
      controls.addEventListener("click", (event) => {
        const button = event.target.closest("button");
        if (!button) return;
        if (button.dataset.view) commit({ ...state, view: button.dataset.view });
        if (button.id === "clear") commit({ ...DEFAULT_STATE });
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

    syncControls();
    apply();
  }

  return {
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
