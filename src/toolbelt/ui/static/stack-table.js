/*
 * Sorting, filtering and the views of the `tt ui` page. Three views (URL
 * `view=table|stacked|grouped`):
 *
 *   table    the stack table, as a tree or flat (`mode=flat`), optionally
 *            compact;
 *   stacked  cards by repo, then stack, in tree order, drawn with rails to
 *            their parents, "stack N of M" chips and slim stubs for the
 *            finished parents kept as context;
 *   grouped  cards in sections by what happens next (Needs me / Waiting on
 *            CI / Waiting on others / Done), flat within a section but
 *            ordered by stack so related branches are adjacent.
 *
 * Each card has its own timeline track (static/pr-timeline.js).
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
  const VIEWS = ["table", "stacked", "grouped"];
  // How the table view lays its rows out.
  const MODES = ["tree", "flat"];
  // Where the compact-table choice is remembered between visits. A
  // convenience only: the URL is the state.
  const COMPACT_STORAGE_KEY = "tt-ui-compact";
  // Which card sections are collapsed (JSON list of section keys); likewise.
  const COLLAPSED_STORAGE_KEY = "tt-ui-collapsed";
  const DEFAULT_STATE = {
    view: "table",
    // Table view only: "tree" (stacks indented) or "flat".
    mode: "tree",
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
  };

  // ---- state <-> query string -------------------------------------------

  // Unknown or retired keys (`view=timeline`, `kind`, `order`, `group`) are
  // ignored, so an old link just opens the default; the retired
  // `view=cards` is the grouped view, and `view=tree|flat` the table.
  function parseState(search) {
    const params = new URLSearchParams(search);
    const state = { ...DEFAULT_STATE };
    const view = params.get("view");
    if (VIEWS.includes(view)) state.view = view;
    else if (view === "cards") state.view = "grouped";
    else if (view === "flat") state.mode = "flat";
    if (MODES.includes(params.get("mode"))) state.mode = params.get("mode");
    const sort = params.get("sort");
    if (COLUMNS.includes(sort)) state.sort = sort;
    if (params.get("dir") === "desc") state.dir = "desc";
    for (const key of ["q", "repo", ...VALUE_FILTERS]) {
      state[key] = params.get(key) || "";
    }
    if (params.get("done") === "1") state.done = "1";
    if (params.get("compact") === "1") state.compact = "1";
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
    if (state.view === "table" && state.mode !== DEFAULT_STATE.mode) {
      params.set("mode", state.mode);
    }
    if (state.sort) {
      params.set("sort", state.sort);
      if (state.dir !== DEFAULT_STATE.dir) params.set("dir", state.dir);
    }
    for (const key of ["q", "repo", ...VALUE_FILTERS]) {
      if (state[key]) params.set(key, state[key]);
    }
    if (state.done) params.set("done", "1");
    if (state.compact) params.set("compact", "1");
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
   * The table in ``flat`` mode is the flat view; the table's tree mode and
   * the stacked cards use the tree layout. (The grouped cards are made from
   * the tree layout too: see ``groupedLayout``.)
   */
  function layout(rows, state) {
    const compare = rowComparator(state);
    if (state.view === "table" && state.mode === "flat") {
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

  // ---- card views -----------------------------------------------------------

  // The sections of the grouped view, in order, keyed by the Next cell's
  // value (who the branch is waiting on).
  const NEXT_SECTIONS = [
    {
      key: "you",
      label: "Needs me",
      tone: "you",
      meaning: "Your move: fix, resolve, publish, request review or merge.",
    },
    {
      key: "ci",
      label: "Waiting on CI",
      tone: "ci",
      meaning: "Checks are running; nothing to do until they finish.",
    },
    {
      key: "others",
      label: "Waiting on others / team",
      tone: "others",
      meaning: "A reviewer, or a parent PR that is waiting on one, has the ball.",
    },
    {
      key: "none",
      label: "Done",
      tone: "none",
      meaning: "Merged or closed.",
    },
    {
      key: "loading",
      label: "Loading",
      tone: "loading",
      meaning: "Status not fetched yet.",
    },
  ];

  /*
   * The grouped view: [{key, label, tone, meaning, entries}] for the
   * sections that have cards, entries as [{id, depth: 0, context: false}].
   * Within a section the cards follow the tree layout (so a stack's branches
   * are adjacent, parents first, with the sort applied among siblings).
   */
  function groupedLayout(rows, state) {
    const byId = new Map(rows.map((row) => [row.id, row]));
    const known = new Set(NEXT_SECTIONS.map((section) => section.key));
    const buckets = new Map(NEXT_SECTIONS.map((section) => [section.key, []]));
    for (const entry of layout(rows, { ...state, view: "stacked" })) {
      if (entry.context) continue;
      const next = byId.get(entry.id).cells.next;
      const who = next ? next.value : "";
      const key = known.has(who) && who !== "loading" ? who : "loading";
      buckets.get(key).push({ id: entry.id, depth: 0, context: false });
    }
    return NEXT_SECTIONS.filter((section) => buckets.get(section.key).length).map(
      (section) => ({ ...section, entries: buckets.get(section.key) }),
    );
  }

  /*
   * The stacked view: one section per repo (in the tree layout's order),
   * with the stack's finished/filtered-out ancestors as context entries.
   */
  function stackedLayout(rows, state) {
    const byId = new Map(rows.map((row) => [row.id, row]));
    const sections = [];
    for (const entry of layout(rows, { ...state, view: "stacked" })) {
      const row = byId.get(entry.id);
      let section = sections[sections.length - 1];
      if (!section || section.repo !== row.repo) {
        section = {
          key: `repo:${row.repo}`,
          repo: row.repo,
          label: row.repoName || row.repo,
          tone: "",
          meaning: "",
          entries: [],
        };
        sections.push(section);
      }
      section.entries.push(entry);
    }
    return sections;
  }

  /*
   * The rails of the stacked view: for each entry ({depth}) in depth-first
   * order, the vertical/elbow segments drawn left of it as
   * [{col, kind}]: column ``c`` belongs to the entry's ancestor (or itself)
   * at depth ``c + 1``. ``line``: that ancestor has later siblings, so the
   * line passes through; ``tee``: the entry itself, with later siblings;
   * ``corner``: the entry itself, the last of its siblings.
   */
  function railGuides(entries) {
    const later = new Map();
    const hasLater = (index) => {
      if (!later.has(index)) {
        const depth = entries[index].depth;
        let found = false;
        for (let j = index + 1; j < entries.length; j += 1) {
          if (entries[j].depth < depth) break;
          if (entries[j].depth === depth) {
            found = true;
            break;
          }
        }
        later.set(index, found);
      }
      return later.get(index);
    };
    const path = [];
    return entries.map((entry, index) => {
      path[entry.depth] = index;
      const segments = [];
      for (let col = 0; col < entry.depth; col += 1) {
        const more = hasLater(path[col + 1]);
        if (col === entry.depth - 1) segments.push({ col, kind: more ? "tee" : "corner" });
        else if (more) segments.push({ col, kind: "line" });
      }
      return segments;
    });
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
    const remember = (value, key = COMPACT_STORAGE_KEY) => {
      try {
        if (storage) storage.setItem(key, value);
      } catch (error) {
        // Remembering is a convenience; ignore a blocked store.
      }
    };
    const collapsed = new Set(
      (() => {
        try {
          return JSON.parse(storage.getItem(COLLAPSED_STORAGE_KEY)) || [];
        } catch (error) {
          return [];
        }
      })(),
    );
    const catalogEl = document.getElementById("state-catalog");
    const catalog = catalogEl ? JSON.parse(catalogEl.textContent) : null;

    let state = initialState(window.location.search, remembered());
    const controls = document.getElementById("controls");
    const table = document.getElementById("stacks");
    const tableWrap = document.getElementById("table-wrap");
    const cardsEl = document.getElementById("cards");
    const cardsWrap = document.getElementById("cards-wrap");
    // Per-card timeline UI state, kept across live updates (the card's DOM
    // is replaced then): card id -> {expanded, hideCommits}.
    const trackState = new Map();
    // Section elements (by key) and stub rows (by branch id) are kept across
    // arrangements, as is which section each card was last in (to flash a
    // card that moved).
    const sectionParts = new Map();
    const stubEls = new Map();
    const lastSection = new Map();
    let arrangedView = null;
    let tooltips = null;

    function syncControls() {
      if (!controls) return;
      for (const key of ["q", "repo", ...VALUE_FILTERS]) {
        const field = controls.elements[key];
        if (field) field.value = state[key];
      }
      for (const button of controls.querySelectorAll("[data-view]")) {
        button.setAttribute("aria-pressed", String(button.dataset.view === state.view));
      }
      for (const button of controls.querySelectorAll("[data-mode]")) {
        button.setAttribute("aria-pressed", String(button.dataset.mode === state.mode));
      }
      if (controls.elements.sort) controls.elements.sort.value = state.sort;
      const toggles = {
        done: state.done === "1",
        compact: state.compact === "1",
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

    // One collapsible section of cards: a sticky header (label, count, what
    // the section means) and a body.
    function sectionFor(key) {
      if (!sectionParts.has(key)) {
        const el = document.createElement("section");
        el.className = "cards-section";
        el.dataset.key = key;
        const heading = document.createElement("h2");
        heading.className = "cards-group";
        const button = document.createElement("button");
        button.type = "button";
        const caret = document.createElement("span");
        caret.className = "cg-caret";
        const label = document.createElement("span");
        label.className = "cg-label";
        const count = document.createElement("span");
        count.className = "cg-count";
        const meaning = document.createElement("span");
        meaning.className = "cg-meaning";
        button.append(caret, label, count, meaning);
        heading.append(button);
        const body = document.createElement("div");
        body.className = "cards-section-body";
        el.append(heading, body);
        sectionParts.set(key, { el, button, label, count, meaning, body });
      }
      return sectionParts.get(key);
    }

    // A slim stand-in for a branch that is not a card of its own but gives
    // the stack below it its context (a finished or filtered-out parent).
    function stubFor(row) {
      if (!stubEls.has(row.id)) {
        const node = document.createElement("div");
        node.className = "card-stub";
        node.dataset.id = row.id;
        stubEls.set(row.id, node);
      }
      const node = stubEls.get(row.id);
      const pr = row.cells.pr || { value: "", sort: "" };
      const text = [pr.sort ? `#${pr.sort}` : "", pr.value || "no PR"].filter(Boolean).join(" ");
      const signature = [row.repoName, row.branch, text].join("|");
      if (node.dataset.signature !== signature) {
        const glyph = document.createElement("span");
        glyph.textContent = "↳";
        const repoChip = document.createElement("span");
        repoChip.className = "repo-chip";
        repoChip.textContent = row.repoName || row.repo;
        const branch = document.createElement("span");
        branch.className = "branch";
        branch.textContent = row.branch;
        const note = document.createElement("span");
        note.textContent = `${text} · kept as context for the stack below`;
        node.replaceChildren(glyph, repoChip, branch, note);
        node.dataset.signature = signature;
      }
      return node;
    }

    // The rails left of a stacked card, from railGuides' segments.
    function setRail(node, segments) {
      const signature = JSON.stringify(segments || []);
      if (node.dataset.rail === signature) return;
      const old = node.querySelector(":scope > .card-rail");
      if (old) old.remove();
      if (segments && segments.length) {
        const rail = document.createElement("div");
        rail.className = "card-rail";
        rail.setAttribute("aria-hidden", "true");
        for (const segment of segments) {
          const seg = document.createElement("i");
          seg.className = "rail-seg";
          seg.dataset.kind = segment.kind;
          seg.style.setProperty("--col", String(segment.col));
          rail.append(seg);
        }
        node.prepend(rail);
      }
      node.dataset.rail = signature;
    }

    // Puts the cards into ``sections`` ([{key, label, tone, meaning,
    // entries}], from stackedLayout / groupedLayout): stacked sections are
    // indented by stack depth with rails and stubs; grouped ones are flat.
    // Cards not in any section are hidden in a pool.
    function arrangeCards(cards, sections, rowsById) {
      const stacked = state.view === "stacked";
      if (arrangedView !== state.view) {
        lastSection.clear();
        arrangedView = state.view;
      }
      const shownIds = new Set();
      const elements = [];
      const placed = new Map(); // card id -> section
      for (const section of sections) {
        const parts = sectionFor(section.key);
        const real = section.entries.filter((entry) => !entry.context);
        parts.label.textContent = section.label;
        parts.count.textContent = String(real.length);
        parts.meaning.textContent = section.meaning;
        parts.el.dataset.tone = section.tone || "";
        const isCollapsed = collapsed.has(section.key);
        parts.el.classList.toggle("collapsed", isCollapsed);
        parts.button.setAttribute("aria-expanded", String(!isCollapsed));
        const guides = stacked ? railGuides(section.entries) : [];
        const nodes = [];
        section.entries.forEach((entry, index) => {
          const row = rowsById.get(entry.id);
          const node = entry.context ? stubFor(row) : cards.get(entry.id);
          if (!node) return;
          if (!entry.context) {
            shownIds.add(entry.id);
            placed.set(entry.id, section);
          }
          node.style.setProperty("--depth", String(stacked ? entry.depth : 0));
          setRail(node, stacked ? guides[index] : null);
          nodes.push(node);
        });
        const current = [...parts.body.children];
        if (nodes.length !== current.length || nodes.some((n, i) => n !== current[i])) {
          parts.body.replaceChildren(...nodes);
        }
        elements.push(parts.el);
      }
      let pool = cardsEl.querySelector(":scope > .cards-pool");
      if (!pool) {
        pool = document.createElement("div");
        pool.className = "cards-pool";
      }
      const hidden = [];
      for (const [id, card] of cards) {
        card.hidden = !shownIds.has(id);
        if (card.hidden) hidden.push(card);
      }
      pool.replaceChildren(...hidden);
      cardsEl.classList.toggle("cards-stacked", stacked);
      const desired = [...elements, pool];
      const current = [...cardsEl.children];
      if (desired.length !== current.length || desired.some((n, i) => n !== current[i])) {
        cardsEl.replaceChildren(...desired);
      }
      // A card whose section changed since the last arrangement moved
      // because its next action did: say so.
      for (const [id, section] of placed) {
        const before = lastSection.get(id);
        if (before !== undefined && before !== section.key) {
          const card = cards.get(id);
          card.classList.add("flash");
          card.addEventListener("animationend", () => card.classList.remove("flash"), {
            once: true,
          });
        }
        lastSection.set(id, section.key);
      }
      for (const id of [...lastSection.keys()]) if (!placed.has(id)) lastSection.delete(id);
      annotateStack(cards, placed, sections, rowsById, stacked);
    }

    // The Parent / Children links say which section the related branch is
    // in when it is not this card's own; the stack chip reads "stack 2 of 4"
    // (and, grouped, which parent it hangs off and where that one is).
    function annotateStack(cards, placed, sections, rowsById, stacked) {
      const stubKeys = new Set();
      for (const section of sections) {
        for (const entry of section.entries) {
          if (entry.context) stubKeys.add(`${section.key}|${entry.id}`);
        }
      }
      for (const [id, card] of cards) {
        const own = placed.get(id);
        if (!own) continue;
        for (const link of card.querySelectorAll("a.stack-link")) {
          const target = link.dataset.target;
          const there = placed.get(target);
          link.removeAttribute("data-in");
          link.removeAttribute("data-gone");
          if (there && there.key !== own.key) link.dataset.in = there.label;
          else if (!there && !stubKeys.has(`${own.key}|${target}`)) link.dataset.gone = "1";
        }
        const chip = card.querySelector(".stack-chip");
        if (!chip) continue;
        const base =
          chip.dataset.height === "1"
            ? "single branch"
            : `stack ${chip.dataset.pos} of ${chip.dataset.height}`;
        let text = base;
        const parent = card.querySelector(".s-parent a.stack-link");
        if (!stacked && parent) {
          const there = placed.get(parent.dataset.target);
          const where = there ? `in ${there.label}` : "not shown";
          text = `${base} · parent: ${parent.textContent} (${where})`;
        }
        if (chip.textContent !== text) chip.textContent = text;
      }
    }

    // Scrolls to a related card (or the stub standing for it in the same
    // section), opening its section if collapsed, and highlights it.
    function jumpTo(id, from) {
      const card = cardsEl.querySelector(`article.card[data-id="${CSS.escape(id)}"]:not([hidden])`);
      let target = card;
      if (!target) {
        const section = from && from.closest(".cards-section");
        target = section && section.querySelector(`.card-stub[data-id="${CSS.escape(id)}"]`);
      }
      if (!target) return;
      const section = target.closest(".cards-section");
      if (section && section.classList.contains("collapsed")) {
        collapsed.delete(section.dataset.key);
        remember(JSON.stringify([...collapsed]), COLLAPSED_STORAGE_KEY);
        section.classList.remove("collapsed");
      }
      target.scrollIntoView({ block: "center", behavior: "smooth" });
      target.classList.remove("highlight");
      // Restart the animation if it is already running.
      void target.offsetWidth;
      target.classList.add("highlight");
      target.addEventListener("animationend", () => target.classList.remove("highlight"), {
        once: true,
      });
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
        const signature = [
          mount.dataset.track,
          ui.expanded,
          ui.hideCommits,
          ui.legend,
          minute,
        ].join("|");
        if (mount.dataset.signature === signature) continue;
        let track;
        try {
          track = JSON.parse(mount.dataset.track);
        } catch (error) {
          continue;
        }
        if (tooltips) tooltips.hide();
        mount.replaceChildren(
          PrTimeline.render(document, track, {
            now,
            catalog,
            expanded: Boolean(ui.expanded),
            hideCommits: Boolean(ui.hideCommits),
            legend: Boolean(ui.legend),
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
      const showTable = state.view === "table";
      if (tableWrap) tableWrap.hidden = !showTable;
      if (cardsWrap) cardsWrap.hidden = showTable;
      table.classList.toggle("compact", state.compact === "1");
      const cards = currentCards();
      syncCardTasks(cards, byId);
      // The table keeps the order and indentation of its own mode in every
      // view (it is hidden in the card views).
      const shown = layout(rows, { ...state, view: "table" });
      const shownIds = new Set(shown.map((entry) => entry.id));
      if (!showTable && cardsEl) {
        const sections =
          state.view === "stacked" ? stackedLayout(rows, state) : groupedLayout(rows, state);
        arrangeCards(cards, sections, new Map(rows.map((row) => [row.id, row])));
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
        if (button.dataset.mode) commit({ ...state, mode: button.dataset.mode });
        if (button.id === "done") commit({ ...state, done: state.done ? "" : "1" });
        if (button.id === "compact") commit({ ...state, compact: state.compact ? "" : "1" });
        if (button.id === "clear") {
          commit({
            ...DEFAULT_STATE,
            view: state.view,
            mode: state.mode,
            compact: state.compact,
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
      if (catalog) tooltips = PrTimeline.attachTooltips(document, window, cardsEl);
      cardsEl.addEventListener("click", (event) => {
        const link = event.target.closest("a.stack-link");
        if (link) {
          event.preventDefault();
          jumpTo(link.dataset.target, link);
          return;
        }
        const head = event.target.closest(".cards-group button");
        if (head) {
          const section = head.closest(".cards-section");
          const key = section.dataset.key;
          if (collapsed.has(key)) collapsed.delete(key);
          else collapsed.add(key);
          remember(JSON.stringify([...collapsed]), COLLAPSED_STORAGE_KEY);
          section.classList.toggle("collapsed", collapsed.has(key));
          head.setAttribute("aria-expanded", String(!collapsed.has(key)));
          return;
        }
        const card = event.target.closest("article.card");
        if (!card) return;
        const ui = { ...(trackState.get(card.dataset.id) || {}) };
        if (event.target.closest(".tl-legend-toggle")) {
          ui.legend = !ui.legend;
        } else if (event.target.closest(".tl-commits")) {
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
      if (state.view !== "table") renderTracks(currentCards());
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
    MODES,
    NEXT_SECTIONS,
    DEFAULT_STATE,
    groupedLayout,
    stackedLayout,
    railGuides,
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
