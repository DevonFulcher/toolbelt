/*
 * The per-card timeline of the `tt ui` page: one PR's life as a horizontal,
 * time-scaled track from its creation to now (or to its merge/close), with a
 * "now" group of labelled state chips at its right end.
 *
 * Everything the track can draw (event glyphs and labels, phase labels,
 * colour tokens, chip texts, the legend) comes from the state catalog
 * (src/toolbelt/ui/pr_states.py, serialized into the page as JSON and passed
 * in as `options.catalog`), so a card state cannot exist without a timeline
 * representation, and the legend cannot drift from what is drawn. Unknown
 * kinds throw rather than render blank.
 *
 * Two layers, like stack-table.js. `trackLayout`, `phaseSpans`, `nowChips`,
 * `tooltipContent`, `positionTip` and `legendModel` are pure and unit-tested
 * with `node --test` (tests/js/pr-timeline.test.mjs). `render` turns a layout
 * into DOM, and `attachTooltips` shows the hover/focus tooltips; the page
 * script (stack-table.js) mounts both into each card and keeps the per-card
 * expanded / hide-commits / legend state.
 *
 * Track data (the card's data-track attribute, from the server):
 *   {created, ended, state, draft, review, events: [{t, kind, actor, detail,
 *    ci, subject}], current: [{dimension, value, args}]}
 * with times as ISO strings, `ended` the merge/close time (or null), `state`
 * the PR state, `draft` whether it is a draft right now, `review` the current
 * review state, and `current` the catalog references for the "now" chips.
 * `created: null` is a branch with no PR (or no history): only the chips.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.PrTimeline = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  const MINUTE = 60 * 1000;
  const HOUR = 60 * MINUTE;
  const DAY = 24 * HOUR;

  // "<1m", "5m", "3h", "4d", "3w": kept in step with pr_activity.format_age.
  function formatAge(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    if (s < 60) return "<1m";
    if (s < 3600) return `${Math.floor(s / 60)}m`;
    if (s < 86400) return `${Math.floor(s / 3600)}h`;
    if (s < 14 * 86400) return `${Math.floor(s / 86400)}d`;
    return `${Math.floor(s / (7 * 86400))}w`;
  }

  // "2d 4h", "3h 12m", "5m", "3w 2d": the two largest non-zero units.
  function formatSpan(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    if (s < 60) return "<1m";
    const units = [
      ["w", 7 * 86400],
      ["d", 86400],
      ["h", 3600],
      ["m", 60],
    ];
    const parts = [];
    let rest = s;
    for (const [name, size] of units) {
      const count = Math.floor(rest / size);
      rest -= count * size;
      if (count > 0 || parts.length > 0) parts.push([count, name]);
      if (parts.length === 2) break;
    }
    return parts
      .filter(([count]) => count > 0)
      .map(([count, name]) => `${count}${name}`)
      .join(" ");
  }

  // "Oct 8, 2026, 3:42 PM" in the viewer's timezone (or `timeZone`).
  function formatAbsolute(ms, options = {}) {
    const text = new Intl.DateTimeFormat("en-US", {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      timeZone: options.timeZone,
    }).format(new Date(ms));
    return text.replace(/[  ]/g, " ");
  }

  const relative = (ms, now) => `${formatSpan((now - ms) / 1000)} ago`;

  // ---- catalog ------------------------------------------------------------

  function eventInfo(catalog, kind) {
    const info = catalog.events[kind];
    if (!info) throw new Error(`timeline: no catalog entry for event kind "${kind}"`);
    return info;
  }

  function phaseInfo(catalog, kind) {
    const info = catalog.phases[kind];
    if (!info) throw new Error(`timeline: no catalog entry for phase kind "${kind}"`);
    return info;
  }

  function stateInfo(catalog, dimension, value) {
    const info = catalog.states.find((s) => s.dimension === dimension && s.value === value);
    if (!info) throw new Error(`timeline: no catalog entry for ${dimension}=${value}`);
    return info;
  }

  // How a server event maps to a marker kind.
  function markerKind(event) {
    if (event.kind === "commit") return event.ci === "failed" ? "commit_failed" : "commit";
    if (event.kind === "review") {
      if (event.detail === "approved") return "review_approved";
      if (event.detail === "requested changes") return "review_changes";
      return "review_commented";
    }
    return event.kind;
  }

  const isCommit = (kind) => kind === "commit" || kind === "commit_failed";

  const clamp01 = (value) => Math.min(1, Math.max(0, value));

  // Which spans of the PR's life were spent in draft, waiting for a
  // reviewer, approved, or with failing CI, as [{kind, t0, t1}] in ms.
  //
  // - draft: from creation (when the first draft toggle is "ready for
  //   review", or the PR is a draft now and was never toggled) or each
  //   "back to draft", until "ready for review".
  // - waiting: not a draft and at least one review request outstanding (a
  //   review by the reviewer, or removing the request, settles it).
  // - approved: from an approving review while any approval stands: until
  //   merge/close/now, a later "changes requested" or dismissal by that
  //   reviewer, or, when the PR is open and not approved now (so the
  //   approval went stale), the first push after it.
  // - ci_failing: from a commit whose CI failed until the next commit or
  //   force push (which supersedes it), or the end of the PR.
  function phaseSpans(events, track, start, end) {
    const toggles = events.filter(
      (e) => e.kind === "ready_for_review" || e.kind === "converted_to_draft",
    );
    const done = track.state === "merged" || track.state === "closed";
    const staleOnPush = !done && Boolean(track.review) && track.review !== "approved";
    let draft = toggles.length ? toggles[0].kind === "ready_for_review" : Boolean(track.draft);
    const pending = new Set();
    const approvers = new Set();
    let failing = false;
    const open = { draft: draft ? start : null, waiting: null, approved: null, ci_failing: null };
    const spans = [];
    const set = (kind, on, ms) => {
      const t = Math.min(Math.max(ms, start), end);
      if (on && open[kind] === null) {
        open[kind] = t;
      } else if (!on && open[kind] !== null) {
        if (t > open[kind]) spans.push({ kind, t0: open[kind], t1: t });
        open[kind] = null;
      }
    };
    for (const event of events) {
      const who = event.subject || event.actor;
      if (event.kind === "ready_for_review") draft = false;
      else if (event.kind === "converted_to_draft") draft = true;
      else if (event.kind === "review_requested") pending.add(who);
      else if (event.kind === "review_request_removed") pending.delete(who);
      else if (event.kind === "review") {
        pending.delete(who);
        if (event.detail === "approved") approvers.add(who);
        else if (event.detail === "requested changes" || event.detail === "review dismissed") {
          approvers.delete(who);
        }
      } else if (event.kind === "commit" || event.kind === "force_push") {
        if (staleOnPush) approvers.clear();
        failing = event.kind === "commit" && event.ci === "failed";
      } else continue;
      set("draft", draft, event.ms);
      set("waiting", pending.size > 0 && !draft, event.ms);
      set("approved", approvers.size > 0, event.ms);
      set("ci_failing", failing, event.ms);
    }
    for (const kind of Object.keys(open)) {
      if (open[kind] !== null && end > open[kind]) {
        spans.push({ kind, t0: open[kind], t1: end });
      }
    }
    return spans.sort((a, b) => a.t0 - b.t0);
  }

  // Axis ticks at "nice" intervals back from the end, labelled with their
  // age. Ticks too near either end (where the end labels are) are dropped.
  const TICK_STEPS = [
    5 * MINUTE, 15 * MINUTE, HOUR, 6 * HOUR, DAY, 3 * DAY, 7 * DAY, 14 * DAY, 28 * DAY,
  ];

  function axisTicks(start, end, now, maxTicks = 6) {
    const span = end - start;
    const step = TICK_STEPS.find((s) => span / s <= maxTicks) || TICK_STEPS[TICK_STEPS.length - 1];
    const ticks = [];
    for (let t = end - step; t > start; t -= step) {
      const x = (t - start) / span;
      if (x > 0.06 && x < 0.94) ticks.push({ x, label: formatAge((now - t) / 1000) });
    }
    return ticks.reverse();
  }

  /*
   * Lays out a track. Options:
   *   catalog    the state catalog (required)
   *   now        current time (ms), required
   *   commits    "none" (no commits at all), "significant" (only commits
   *              whose CI failed) or "all" (ordinary commits as faint dots,
   *              clustered where they would overlap)
   *   minGap     fraction of the track under which commits cluster (0.012)
   *   laneGap    fraction of the track two prominent markers need between
   *              them to share a lane (more when labels are drawn)
   *   maxLanes   most lanes markers stack into (3)
   *
   * Every marker and phase carries `tip`, the data its hover tooltip is
   * built from (see `tooltipContent`).
   */
  function trackLayout(track, options) {
    const { now, catalog } = options;
    const commits = options.commits || "significant";
    const minGap = options.minGap === undefined ? 0.012 : options.minGap;
    const laneGap = options.laneGap === undefined ? 0.015 : options.laneGap;
    const maxLanes = options.maxLanes || 3;

    const start = Date.parse(track.created);
    const events = (track.events || [])
      .map((event, seq) => ({ ...event, ms: Date.parse(event.t), seq }))
      .sort((a, b) => a.ms - b.ms || a.seq - b.seq);
    const done = track.state === "merged" || track.state === "closed";
    let endMs = track.ended ? Date.parse(track.ended) : null;
    if (endMs === null && done) {
      // A finished PR whose end event fell out of the history we have.
      const last = events.length ? events[events.length - 1].ms : start;
      endMs = Math.min(Math.max(last, start), now);
    }
    const ended = endMs !== null;
    const end = ended ? Math.max(endMs, start) : Math.max(now, start);
    const span = Math.max(end - start, MINUTE);
    const xOf = (ms) => clamp01((ms - start) / span);
    const age = (ms) => `${formatAge((now - ms) / 1000)} ago`;
    const endKind = ended ? (track.state === "closed" ? "closed" : "merged") : null;

    // One marker per event, then drop and cluster plain commits.
    const prominent = [];
    const plain = [];
    for (const event of events) {
      const kind = markerKind(event);
      const info = eventInfo(catalog, kind);
      if (isCommit(kind) && commits === "none") continue;
      const marker = {
        kind,
        x: xOf(event.ms),
        ms: event.ms,
        t: event.t,
        actor: event.actor,
        detail: event.detail,
        ci: event.ci || "",
        count: 1,
        lane: 0,
        items: [{ t: event.t, ms: event.ms, actor: event.actor, detail: event.detail }],
      };
      marker.title =
        `${info.label}${event.detail && kind !== "opened" && event.kind !== "review" ? `: ${event.detail}` : ""}` +
        `${event.actor ? ` (${event.actor})` : ""} ${age(event.ms)}`;
      marker.tip = {
        type: "event",
        kind,
        label: info.label,
        detail: event.detail,
        actor: event.actor,
        subject: event.subject || "",
        ci: event.ci || "",
        ms: event.ms,
      };
      if (kind === "commit") {
        if (commits === "all") plain.push(marker);
      } else {
        prominent.push(marker);
      }
    }

    const clusters = [];
    for (const marker of plain) {
      const previous = clusters[clusters.length - 1];
      if (previous && marker.x - previous.lastX < minGap) {
        previous.members.push(marker);
        previous.lastX = marker.x;
      } else {
        clusters.push({ members: [marker], lastX: marker.x });
      }
    }
    const faint = clusters.map(({ members }) => {
      if (members.length === 1) return members[0];
      const first = members[0];
      const x = members.reduce((sum, m) => sum + m.x, 0) / members.length;
      const items = members.flatMap((m) => m.items);
      return {
        ...first,
        kind: "commits",
        x,
        count: members.length,
        detail: `${members.length} commits`,
        items,
        title: `${members.length} commits, ${age(first.ms)} to ${age(members[members.length - 1].ms)}`,
        tip: {
          type: "cluster",
          label: `${members.length} commits`,
          items,
          first: first.ms,
          last: members[members.length - 1].ms,
        },
      };
    });

    // Prominent markers that would overlap stack into lanes.
    const lastX = [];
    for (const marker of prominent) {
      let lane = lastX.findIndex((x) => marker.x - x >= laneGap);
      if (lane === -1) {
        lane = lastX.length < maxLanes ? lastX.length : lastX.indexOf(Math.min(...lastX));
      }
      lastX[lane] = marker.x;
      marker.lane = lane;
    }

    const phases = phaseSpans(events, track, start, end).map((p) => {
      const info = phaseInfo(catalog, p.kind);
      return {
        kind: p.kind,
        label: info.label,
        x0: xOf(p.t0),
        x1: xOf(p.t1),
        t0: new Date(p.t0).toISOString(),
        t1: new Date(p.t1).toISOString(),
        title: `${info.label} for ${formatAge((p.t1 - p.t0) / 1000)}`,
        tip: {
          type: "phase",
          kind: p.kind,
          label: info.label,
          legend: info.legend,
          t0: p.t0,
          t1: p.t1,
          ongoing: !ended && p.t1 >= end,
        },
      };
    });

    const markers = [...prominent, ...faint].sort((a, b) => a.x - b.x || a.ms - b.ms);
    return {
      start,
      end,
      span,
      ended,
      markers,
      phases,
      lanes: Math.max(1, lastX.length),
      now: ended ? null : 1,
      startLabel: `opened ${age(start)}`,
      endLabel: ended ? `${endKind} ${age(end)}` : "now",
      ticks: axisTicks(start, end, now),
    };
  }

  const WHO_TONES = { you: "accent", ci: "amber", others: "gray", none: "gray" };

  // The "now" chips: [{group, dimension, text, tone, title}], each looked up
  // in the catalog (an unknown value throws). The Next chip is toned by who
  // it waits on and says the action's own label (which names a parent).
  function nowChips(track, catalog) {
    return (track.current || []).map((ref) => {
      const info = stateInfo(catalog, ref.dimension, ref.value);
      const args = ref.args || {};
      let text = info.label.replace("{n}", args.n === undefined ? "" : String(args.n));
      let tone = info.tone;
      if (ref.dimension === "next") {
        text = args.text || text;
        tone = WHO_TONES[args.who] || tone;
      }
      const group = catalog.chips[ref.dimension];
      if (!group) throw new Error(`timeline: no chip group for "${ref.dimension}"`);
      return { dimension: ref.dimension, group, text, tone, title: info.legend };
    });
  }

  // ---- tooltips -----------------------------------------------------------

  /*
   * What a marker's / span's tooltip says, as {title, subtitle, rows, items}
   * with the absolute time (viewer's timezone) and the relative one.
   * `tip` is a marker's or phase's `tip` from trackLayout.
   */
  function tooltipContent(tip, now, options = {}) {
    const abs = (ms) => formatAbsolute(ms, options);
    const both = (ms) => `${abs(ms)} (${relative(ms, now)})`;
    if (tip.type === "event") {
      const rows = [];
      if (tip.actor) rows.push(["Actor", tip.actor]);
      if (tip.subject && tip.subject !== tip.actor) rows.push(["Reviewer", tip.subject]);
      if (tip.ci) rows.push(["CI", tip.ci]);
      rows.push(["When", abs(tip.ms)], ["Ago", relative(tip.ms, now)]);
      return {
        title: tip.label,
        subtitle: tip.detail && tip.detail !== tip.label ? tip.detail : "",
        rows,
        items: [],
      };
    }
    if (tip.type === "cluster") {
      const shown = tip.items.slice(0, 8);
      const items = shown.map(
        (item) => `${abs(item.ms)} · ${item.actor || "?"} · ${item.detail || ""}`,
      );
      if (tip.items.length > shown.length) items.push(`+ ${tip.items.length - shown.length} more`);
      return {
        title: tip.label,
        subtitle: "",
        rows: [
          ["From", both(tip.first)],
          ["To", both(tip.last)],
        ],
        items,
      };
    }
    return {
      title: tip.label,
      subtitle: tip.legend,
      rows: [
        ["From", both(tip.t0)],
        ["To", tip.ongoing ? "now" : both(tip.t1)],
        ["Duration", formatSpan((tip.t1 - tip.t0) / 1000)],
      ],
      items: [],
    };
  }

  // Where to put a tooltip of `size` next to `anchor` (a viewport rect),
  // below it when it fits (else above), centred and clamped to the viewport.
  function positionTip(anchor, size, viewport, gap = 8) {
    const margin = 4;
    const centre = (anchor.left + anchor.right) / 2;
    let left = centre - size.width / 2;
    left = Math.min(left, viewport.width - size.width - margin);
    left = Math.max(left, margin);
    const below = anchor.bottom + gap;
    const above = anchor.top - gap - size.height;
    let top = below;
    let placement = "below";
    if (below + size.height > viewport.height - margin && above >= margin) {
      top = above;
      placement = "above";
    }
    top = Math.max(margin, Math.min(top, viewport.height - size.height - margin));
    return { left, top, placement };
  }

  // ---- legend ---------------------------------------------------------------

  // Everything the track and chips can show, from the catalog, as
  // {events, phases, chips: [{group, values: [{text, tone, legend}]}],
  // notRepresented}.
  function legendModel(catalog) {
    return {
      events: Object.entries(catalog.events).map(([kind, info]) => ({ kind, ...info })),
      phases: Object.entries(catalog.phases).map(([kind, info]) => ({ kind, ...info })),
      chips: Object.entries(catalog.chips).map(([dimension, group]) => ({
        dimension,
        group,
        values: catalog.states
          .filter((s) => s.dimension === dimension)
          .map((s) => ({ text: s.label, tone: s.tone, legend: s.legend })),
      })),
      notRepresented: catalog.not_represented,
    };
  }

  // ---- list -----------------------------------------------------------------

  // The plain chronological list: every event (commits unless "none"),
  // oldest first.
  function listEntries(track, options) {
    const { now, catalog } = options;
    const hideCommits = options.commits === "none";
    return (track.events || [])
      .map((event, seq) => ({ event, seq, ms: Date.parse(event.t) }))
      .sort((a, b) => a.ms - b.ms || a.seq - b.seq)
      .filter(({ event }) => !(hideCommits && event.kind === "commit"))
      .map(({ event, ms }) => {
        const kind = markerKind(event);
        return {
          t: event.t,
          ms,
          kind,
          label: eventInfo(catalog, kind).label,
          actor: event.actor,
          detail: event.detail,
          ci: event.ci || "",
          age: relative(ms, now),
        };
      });
  }

  // ---- DOM ----------------------------------------------------------------

  function el(document, tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  const CI_WORDS = { failed: "failed", success: "passed", pending: "pending" };

  function renderLegend(document, catalog) {
    const model = legendModel(catalog);
    const box = el(document, "div", "tl-legend");
    const section = (title, extra = "") => {
      const part = el(document, "section", `tl-legend-part${extra}`);
      part.append(el(document, "h4", "", title));
      box.append(part);
      return part;
    };
    const row = (part, sample, name, text) => {
      const line = el(document, "div", "tl-legend-row");
      line.append(sample, el(document, "b", "", name), el(document, "span", "", text));
      part.append(line);
    };

    const events = section("Markers");
    for (const info of model.events) {
      const sample =
        info.kind === "commits"
          ? el(document, "span", `tl-sample tl-k-commits tone-${info.tone}`, "N")
          : el(document, "span", `tl-sample tone-${info.tone}`, info.glyph);
      row(events, sample, info.label, info.legend);
    }
    const phases = section("Shaded spans");
    for (const info of model.phases) {
      row(phases, el(document, "span", `tl-sample tl-swatch tone-${info.tone}`), info.label, info.legend);
    }
    const chips = section("Now (chips at the right end)", " tl-legend-chips");
    for (const group of model.chips) {
      for (const value of group.values) {
        const chip = el(document, "span", `tl-sample tl-chip tone-${value.tone}`);
        chip.append(el(document, "span", "tl-chip-label", group.group), ` ${value.text}`);
        row(chips, chip, "", value.legend);
      }
    }
    const not = section("Not shown");
    for (const item of model.notRepresented) {
      row(not, el(document, "span", "tl-sample", "–"), item.label, item.why);
    }
    return box;
  }

  /*
   * Builds the DOM for one card's timeline. `options`: catalog, now (ms),
   * expanded, hideCommits, legend. Collapsed: a slim track with the
   * prominent events. Expanded: a taller track with labelled events, faint
   * commit dots (unless hideCommits) and the chronological table. The "now"
   * chips are at the right end in both; the legend toggles with "?".
   */
  function render(document, track, options) {
    const { now, expanded, hideCommits, catalog } = options;
    const root = el(document, "div", `tl ${expanded ? "tl-expanded" : "tl-collapsed"}`);
    const body = el(document, "div", "tl-body");
    const left = el(document, "div", "tl-left");
    const chips = el(document, "div", "tl-chips");
    chips.setAttribute("aria-label", "Current state");
    for (const chip of nowChips(track, catalog)) {
      const node = el(document, "span", `tl-chip tone-${chip.tone} tl-chip-${chip.dimension}`);
      node.append(el(document, "span", "tl-chip-label", chip.group), ` ${chip.text}`);
      node.title = chip.title;
      chips.append(node);
    }

    const buttons = el(document, "span", "tl-buttons");
    const legendButton = el(document, "button", "tl-legend-toggle", "?");
    legendButton.type = "button";
    legendButton.title = "What do the glyphs, colours and spans mean?";
    legendButton.setAttribute("aria-pressed", String(Boolean(options.legend)));

    if (track.created === null || track.created === undefined) {
      // No PR yet, or no history for it: nothing to put on a time axis.
      left.append(
        el(
          document,
          "div",
          "tl-ends",
          track.state === "none" ? "no PR yet: no history to draw" : "PR history unavailable",
        ),
      );
      buttons.append(legendButton);
      left.append(buttons);
      body.append(left, chips);
      root.append(body);
      if (options.legend) root.append(renderLegend(document, catalog));
      return root;
    }

    const commits = hideCommits ? "none" : expanded ? "all" : "significant";
    const layout = trackLayout(track, {
      now,
      catalog,
      commits,
      laneGap: expanded ? 0.15 : 0.015,
      maxLanes: expanded ? 4 : 2,
    });
    const laneStep = expanded ? 20 : 8;
    const base = expanded ? 34 : 24;
    const height = base + (layout.lanes - 1) * laneStep;
    root.style.setProperty("--tl-height", `${height}px`);

    const head = el(document, "div", "tl-ends");
    const expand = el(document, "button", "tl-expand", expanded ? "Collapse" : "Expand");
    expand.type = "button";
    expand.setAttribute("aria-expanded", String(Boolean(expanded)));
    buttons.append(expand);
    if (expanded) {
      const toggle = el(document, "button", "tl-commits", hideCommits ? "Show commits" : "Hide commits");
      toggle.type = "button";
      toggle.setAttribute("aria-pressed", String(Boolean(hideCommits)));
      buttons.append(toggle);
    }
    buttons.append(legendButton);
    head.append(
      el(document, "span", "tl-start", layout.startLabel),
      buttons,
      el(document, "span", "tl-end", layout.endLabel),
    );

    const trackEl = el(document, "div", "tl-track");
    trackEl.append(el(document, "div", "tl-axis"));
    for (const tick of layout.ticks) {
      const node = el(document, "span", "tl-tick", expanded ? tick.label : "");
      node.style.left = `${tick.x * 100}%`;
      trackEl.append(node);
    }
    for (const phase of layout.phases) {
      const tone = phaseInfo(catalog, phase.kind).tone;
      const node = el(document, "span", `tl-phase tl-phase-${phase.kind} tone-${tone} tl-tip-src`);
      node.style.left = `${phase.x0 * 100}%`;
      node.style.width = `${Math.max((phase.x1 - phase.x0) * 100, 0.4)}%`;
      node.tabIndex = 0;
      node.setAttribute("aria-label", phase.title);
      node.ttTip = phase.tip;
      trackEl.append(node);
    }
    for (const marker of layout.markers) {
      const faint = marker.kind === "commit" || marker.kind === "commits";
      const info = eventInfo(catalog, marker.kind);
      const node = el(
        document,
        "span",
        `tl-mark tl-k-${marker.kind} tone-${info.tone} tl-tip-src${faint ? " tl-faint" : ""}`,
        marker.kind === "commits" ? String(marker.count) : info.glyph,
      );
      node.style.left = `${marker.x * 100}%`;
      node.tabIndex = 0;
      node.setAttribute("aria-label", marker.title);
      node.ttTip = marker.tip;
      if (!faint) {
        node.style.top = `${(expanded ? 14 : 12) + marker.lane * laneStep}px`;
      }
      if (expanded && !faint) {
        const label = el(
          document,
          "span",
          `tl-label${marker.x > 0.78 ? " tl-flip" : ""}`,
          marker.actor && marker.kind.startsWith("review")
            ? `${info.label} · ${marker.actor}`
            : info.label,
        );
        node.append(label);
      }
      trackEl.append(node);
    }
    if (layout.now !== null) trackEl.append(el(document, "span", "tl-now"));
    trackEl.style.height = `${height}px`;
    left.append(head, trackEl);
    body.append(left, chips);
    root.append(body);

    if (options.legend) root.append(renderLegend(document, catalog));

    if (expanded) {
      const entries = listEntries(track, { now, commits, catalog });
      const hasCi = entries.some((entry) => entry.ci);
      const wrap = el(document, "div", `tl-list-wrap${hasCi ? " tl-has-ci" : ""}`);
      const table = el(document, "table", "tl-list");
      const headRow = el(document, "tr", "");
      const headings = ["Time", "Event", "Actor", "Detail"];
      if (hasCi) headings.push("CI");
      for (const text of headings) headRow.append(el(document, "th", `tl-h-${text.toLowerCase()}`, text));
      const thead = el(document, "thead", "");
      thead.append(headRow);
      const tbody = el(document, "tbody", "");
      for (const entry of entries) {
        const tr = el(document, "tr", `tl-item tl-i-${entry.kind}`);
        const when = el(document, "td", "tl-when");
        const time = el(document, "time", "", entry.age);
        time.dateTime = entry.t;
        time.title = formatAbsolute(entry.ms);
        when.append(time);
        const detail = el(document, "td", "tl-detail", entry.detail);
        detail.title = entry.detail;
        tr.append(
          when,
          el(document, "td", "tl-what", entry.label),
          el(document, "td", "tl-who", entry.actor),
          detail,
        );
        if (hasCi) {
          tr.append(el(document, "td", `tl-ci tl-ci-${entry.ci || "none"}`, CI_WORDS[entry.ci] || ""));
        }
        tbody.append(tr);
      }
      table.append(thead, tbody);
      wrap.append(table);
      root.append(wrap);
    }
    return root;
  }

  /*
   * Shows a tooltip immediately when a marker, cluster or span (anything
   * `.tl-tip-src` inside `container`) is hovered or focused, and hides it on
   * leave/blur/Escape/scroll. One tooltip element, kept inside the viewport.
   * `catalog` is not needed: the content was built into each node's `ttTip`.
   */
  function attachTooltips(document, window, container) {
    let tip = null;
    let anchor = null;
    const ensure = () => {
      if (!tip) {
        tip = el(document, "div", "tl-tip");
        tip.id = "tl-tip";
        tip.setAttribute("role", "tooltip");
        tip.hidden = true;
        document.body.append(tip);
      }
      return tip;
    };
    const hide = () => {
      if (anchor && anchor.removeAttribute) anchor.removeAttribute("aria-describedby");
      anchor = null;
      if (tip) tip.hidden = true;
    };
    const show = (source) => {
      if (!source.ttTip) return;
      const node = ensure();
      const content = tooltipContent(source.ttTip, Date.now());
      node.replaceChildren();
      node.append(el(document, "div", "tl-tip-title", content.title));
      if (content.subtitle) node.append(el(document, "div", "tl-tip-sub", content.subtitle));
      for (const [key, value] of content.rows) {
        const row = el(document, "div", "tl-tip-row");
        row.append(el(document, "span", "tl-tip-key", key), el(document, "span", "tl-tip-val", value));
        node.append(row);
      }
      if (content.items.length) {
        const list = el(document, "ul", "tl-tip-items");
        for (const text of content.items) list.append(el(document, "li", "", text));
        node.append(list);
      }
      node.hidden = false;
      node.style.left = "0px";
      node.style.top = "0px";
      const rect = source.getBoundingClientRect();
      const box = node.getBoundingClientRect();
      const place = positionTip(
        rect,
        { width: box.width, height: box.height },
        { width: window.innerWidth, height: window.innerHeight },
      );
      node.style.left = `${place.left}px`;
      node.style.top = `${place.top}px`;
      node.dataset.placement = place.placement;
      source.setAttribute("aria-describedby", "tl-tip");
      anchor = source;
    };
    const sourceOf = (event) =>
      event.target.closest ? event.target.closest(".tl-tip-src") : null;
    container.addEventListener("mouseover", (event) => {
      const source = sourceOf(event);
      if (source && source !== anchor) show(source);
      else if (!source) hide();
    });
    container.addEventListener("mouseleave", hide);
    container.addEventListener("focusin", (event) => {
      const source = sourceOf(event);
      if (source) show(source);
    });
    container.addEventListener("focusout", hide);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") hide();
    });
    // Scrolling (also what keyboard focus does to bring a marker into view)
    // moves the anchor: follow it, or hide when it is gone.
    window.addEventListener(
      "scroll",
      () => {
        if (anchor && anchor.isConnected) show(anchor);
        else hide();
      },
      true,
    );
    return { hide };
  }

  return {
    formatAge,
    formatSpan,
    formatAbsolute,
    markerKind,
    phaseSpans,
    axisTicks,
    trackLayout,
    nowChips,
    tooltipContent,
    positionTip,
    legendModel,
    listEntries,
    render,
    attachTooltips,
  };
});
