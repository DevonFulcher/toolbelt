/*
 * Sorting, filtering and the views of the `tt ui` page: the stack table (tree
 * or flat, optionally compact) and a vertical list of cards, one per branch,
 * each with its own timeline track (static/pr-timeline.js).
 *
 * The table is the source of truth: its cells carry the sort/filter values,
 * and the cards are laid out from the same rows (a card is matched to its row
 * by id; its live half carries the branch's timeline data).
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
  const api = factory(root);
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
})(typeof self !== "undefined" ? self : this, function (root) {
  const PrTimeline =
    typeof require === "function" ? require("./pr-timeline.js") : root.PrTimeline;

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
  const VIEWS = ["tree", "flat", "cards"];
  // Where the compact-table choice is remembered between visits. A
  // convenience only: the URL is the state.
  const COMPACT_STORAGE_KEY = "tt-ui-compact";
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
    // "1" includes merged/closed branches, which are hidden by default.
    done: "",
    // "1" collapses the PR/CI/Review/Comments/Merge columns into one.
    compact: "",
    // "repo" groups cards by repo and stack (default: one flat sorted list).
    group: "",
  };

  // ---- state <-> query string -------------------------------------------

  // Unknown or retired keys (the old timeline view's `view=timeline`,
  // `kind`, `order`) are ignored, so an old link just opens the default.
  function parseState(search) {
    const params = new URLSearchParams(search);
    const state = { ...DEFAULT_STATE };
    const view = params.get("view");
    if (VIEWS.includes(view)) state.view = view;
    const sort = params.get("sort");
    if (COLUMNS.includes(sort)) state.sort = sort;
    if (params.get("dir") === "desc") state.dir = "desc";
    for (const key of ["q", "repo", ...VALUE_FILTERS]) {
      state[key] = params.get(key) || "";
    }
    if (params.get("done") === "1") state.done = "1";
    if (params.get("compact") === "1") state.compact = "1";
    if (params.get("group") === "repo") state.group = "repo";
    return state;
  }

  // The state a page load starts with: the URL's, except that the remembered
  // compact choice applies when the URL does not say.
  function initialState(search, storedCompact) {
    const state = parseState(search);
    if (!new URLSearchParams(search).has("compact") && storedCompact === "1") {
      state.compact = "1";
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
    if (state.done) params.set("done", "1");
    if (state.compact) params.set("compact", "1");
    if (state.group) params.set("group", state.group);
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

  // A merged/closed branch ("done": its next action's who is "none") is
  // hidden unless the Include done toggle is on or the Next filter asks for
  // done branches.
  function hiddenAsDone(row, state) {
    if (state.done === "1" || state.next === "none") return false;
    return Boolean(row.cells.next) && row.cells.next.value === "none";
  }

  // Whether a row passes the repo, done, text and per-column filters. Every
  // whitespace-separated term of the text filter must appear.
  function matches(row, state) {
    if (!inScope(row, state) || hiddenAsDone(row, state)) return false;
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
   * The rows to show, in order, as [{id, depth, context}].
   *
   * rows: [{id, repo, parent, order, search, cells: {col: {value, sort}}}]
   * for the whole table (ids are unique across repos; the trunk is not a
   * row, so a stack's first branch has no parent row and is a root). Flat
   * view: the matching rows, sorted. Tree view: the same rows plus their
   * ancestors (kept for context, flagged ``context``), depth-first, with the
   * sort applied among siblings; each repo's rows stay together, and the
   * repos themselves are in server (name) order, or by the Repo column when
   * it is the sort.
   *
   * Cards are a flat, sorted list (by default the rows that need the user
   * come first), or with group=repo each repo's stacks in tree order.
   */
  function layout(rows, state) {
    if (state.view === "cards") {
      state = {
        ...state,
        view: state.group === "repo" ? "tree" : "flat",
        sort: state.sort || "next",
      };
    }
    const compare = rowComparator(state);
    if (state.view === "flat") {
      return rows
        .filter((row) => matches(row, state))
        .sort(compare)
        .map((row) => ({ id: row.id, depth: 0, context: false }));
    }

    const byId = new Map(rows.map((row) => [row.id, row]));
    const matched = new Set();
    const keep = new Set();
    for (const row of rows) {
      if (!matches(row, state)) continue;
      matched.add(row.id);
      for (let r = row; r && !keep.has(r.id); r = byId.get(r.parent)) {
        keep.add(r.id);
      }
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
      out.push({ id: row.id, depth, context: !matched.has(row.id) });
      for (const child of (children.get(row.id) || []).sort(compare)) {
        visit(child, depth + 1);
      }
    };
    for (const group of repoGroups) {
      for (const row of group.sort(compare)) visit(row, 0);
    }
    return out;
  }

  // ---- ages ---------------------------------------------------------------

  const formatAge = PrTimeline.formatAge;

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
      parent: tr.dataset.parent || null,
      order: Number(tr.dataset.order),
      search: search.join(" "),
      cells,
    };
  }

  function init(document, window) {
    const storage = (() => {
      try {
        return window.localStorage;
      } catch (error) {
        return null;
      }
    })();
    const remembered = () => {
      try {
        return storage ? storage.getItem(COMPACT_STORAGE_KEY) : null;
      } catch (error) {
        return null;
      }
    };
    const remember = (value) => {
      try {
        if (storage) storage.setItem(COMPACT_STORAGE_KEY, value);
      } catch (error) {
        // Remembering is a convenience; ignore a blocked store.
      }
    };

    let state = initialState(window.location.search, remembered());
    const controls = document.getElementById("controls");
    const table = document.getElementById("stacks");
    const tableWrap = document.getElementById("table-wrap");
    const cardsEl = document.getElementById("cards");
    const cardsWrap = document.getElementById("cards-wrap");
    // Per-card timeline UI state, kept across live updates (the card's DOM
    // is replaced then): card id -> {expanded, hideCommits}.
    const trackState = new Map();
    const groupHeaders = new Map();

    function syncControls() {
      if (!controls) return;
      for (const key of ["q", "repo", ...VALUE_FILTERS]) {
        const field = controls.elements[key];
        if (field) field.value = state[key];
      }
      for (const button of controls.querySelectorAll("[data-view]")) {
        button.setAttribute("aria-pressed", String(button.dataset.view === state.view));
      }
      if (controls.elements.sort) controls.elements.sort.value = state.sort;
      const toggles = {
        done: state.done === "1",
        compact: state.compact === "1",
        group: state.group === "repo",
      };
      for (const [id, on] of Object.entries(toggles)) {
        const button = controls.querySelector(`#${id}`);
        if (button) button.setAttribute("aria-pressed", String(on));
      }
      // Controls that only mean something in some views.
      for (const el of controls.querySelectorAll("[data-views]")) {
        el.hidden = !el.dataset.views.split(" ").includes(state.view);
      }
    }

    // A card's task slot shows what the table's task cell shows (the title
    // and status arrive there by htmx), so each task is looked up once. A
    // branch without a task keeps the card's own "no task linked" note.
    function syncCardTasks(cards, byId) {
      for (const [id, card] of cards) {
        const td = byId.get(id) && byId.get(id).querySelector("td.col-task");
        const slot = card.querySelector(".card-task");
        if (!td || !slot || !td.dataset.value) continue;
        if (slot.innerHTML !== td.innerHTML) slot.innerHTML = td.innerHTML;
      }
    }

    function groupHeader(row) {
      if (!groupHeaders.has(row.repo)) {
        const header = document.createElement("h2");
        header.className = "cards-group";
        header.textContent = row.repoName;
        groupHeaders.set(row.repo, header);
      }
      return groupHeaders.get(row.repo);
    }

    // Puts the cards in the order of ``shown`` (with repo headings when
    // grouped, each card indented by stack depth then), hiding the rest.
    function arrangeCards(cards, shown, rowsById) {
      const grouped = state.group === "repo";
      const shownIds = new Set();
      const desired = [];
      let repo = null;
      for (const entry of shown) {
        if (entry.context) continue;
        const card = cards.get(entry.id);
        if (!card) continue;
        shownIds.add(entry.id);
        const row = rowsById.get(entry.id);
        if (grouped && row.repo !== repo) {
          repo = row.repo;
          desired.push(groupHeader(row));
        }
        card.style.setProperty("--depth", String(grouped ? entry.depth : 0));
        desired.push(card);
      }
      for (const [id, card] of cards) {
        card.hidden = !shownIds.has(id);
        if (card.hidden) desired.push(card);
      }
      const current = [...cardsEl.children];
      if (
        desired.length !== current.length ||
        desired.some((node, index) => node !== current[index])
      ) {
        cardsEl.replaceChildren(...desired);
      }
    }

    // Re-draws each visible card's timeline track when its data, its UI
    // state or the minute changed.
    function renderTracks(cards) {
      const now = Date.now();
      const minute = Math.floor(now / 60000);
      for (const [id, card] of cards) {
        if (card.hidden) continue;
        const mount = card.querySelector(".tl-mount");
        if (!mount) continue;
        const ui = trackState.get(id) || {};
        const signature = [mount.dataset.track, ui.expanded, ui.hideCommits, minute].join("|");
        if (mount.dataset.signature === signature) continue;
        let track;
        try {
          track = JSON.parse(mount.dataset.track);
        } catch (error) {
          continue;
        }
        mount.replaceChildren(
          PrTimeline.render(document, track, {
            now,
            expanded: Boolean(ui.expanded),
            hideCommits: Boolean(ui.hideCommits),
          }),
        );
        mount.dataset.signature = signature;
      }
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

    function currentCards() {
      return new Map(
        cardsEl
          ? [...cardsEl.querySelectorAll("article.card")].map((card) => [card.dataset.id, card])
          : [],
      );
    }

    function apply() {
      if (!table) return;
      const tbody = table.querySelector("tbody");
      const trs = [...tbody.querySelectorAll("tr")];
      const byId = new Map(trs.map((tr) => [tr.dataset.id, tr]));
      const rows = trs.map(readRow);
      const showTable = state.view === "tree" || state.view === "flat";
      if (tableWrap) tableWrap.hidden = !showTable;
      if (cardsWrap) cardsWrap.hidden = state.view !== "cards";
      table.classList.toggle("compact", state.compact === "1");
      const cards = currentCards();
      syncCardTasks(cards, byId);
      const shown = layout(rows, state);
      const shownIds = new Set(shown.map((entry) => entry.id));
      if (state.view === "cards" && cardsEl) {
        arrangeCards(cards, shown, new Map(rows.map((row) => [row.id, row])));
        renderTracks(cards);
      }

      const desired = shown.map((entry) => byId.get(entry.id));
      for (const tr of trs) if (!shownIds.has(tr.dataset.id)) desired.push(tr);
      shown.forEach((entry) => {
        const tr = byId.get(entry.id);
        tr.hidden = false;
        tr.classList.toggle("context", entry.context);
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
      const inRepo = rows.filter((row) => inScope(row, state));
      const visible = rows.filter((row) => matches(row, state)).length;
      const doneHidden = inRepo.filter((row) => hiddenAsDone(row, state)).length;
      const count = document.getElementById("count");
      if (count) {
        count.textContent =
          `${visible} of ${inRepo.length} branches` +
          (doneHidden ? ` (${doneHidden} done hidden)` : "");
      }
      const none = document.getElementById("no-match");
      if (none) none.hidden = visible > 0 || inRepo.length === 0;
      refreshAges();
    }

    function commit(next) {
      if (next.compact !== state.compact) remember(next.compact ? "1" : "0");
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
        if (button.id === "done") commit({ ...state, done: state.done ? "" : "1" });
        if (button.id === "compact") commit({ ...state, compact: state.compact ? "" : "1" });
        if (button.id === "group") commit({ ...state, group: state.group ? "" : "repo" });
        if (button.id === "clear") {
          commit({
            ...DEFAULT_STATE,
            view: state.view,
            compact: state.compact,
            group: state.group,
          });
        }
      });
    }
    document.addEventListener("click", (event) => {
      const th = event.target.closest("thead th[data-col]");
      // The compact table's combined Status column is not sortable.
      if (th && COLUMNS.includes(th.dataset.col)) commit(toggleSort(state, th.dataset.col));
    });
    if (cardsEl) {
      cardsEl.addEventListener("click", (event) => {
        const card = event.target.closest("article.card");
        if (!card) return;
        const ui = { ...(trackState.get(card.dataset.id) || {}) };
        if (event.target.closest(".tl-commits")) {
          ui.hideCommits = !ui.hideCommits;
        } else if (
          event.target.closest(".tl-expand") ||
          (!ui.expanded && event.target.closest(".tl-track"))
        ) {
          ui.expanded = !ui.expanded;
        } else {
          return;
        }
        trackState.set(card.dataset.id, ui);
        renderTracks(currentCards());
      });
    }

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

    window.setInterval(() => {
      refreshAges();
      if (state.view === "cards") renderTracks(currentCards());
    }, 30000);

    // A stale or hand-edited URL (e.g. the retired view=timeline) is
    // rewritten to the state it was understood as.
    const normalized = serializeState(state);
    if (normalized !== window.location.search) {
      window.history.replaceState(null, "", window.location.pathname + normalized);
    }
    syncControls();
    apply();
  }

  return {
    VIEWS,
    formatAge,
    COLUMNS,
    VALUE_FILTERS,
    DEFAULT_STATE,
    parseState,
    initialState,
    serializeState,
    toggleSort,
    compareKeys,
    matches,
    layout,
    init,
  };
});
