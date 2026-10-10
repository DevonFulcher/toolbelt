/*
 * The per-card timeline of the `tt ui` page: one PR's life as a horizontal,
 * time-scaled track from its creation to now (or to its merge/close).
 *
 * Two layers, like stack-table.js. `trackLayout` is pure: given a card's
 * track data and the current time it computes where every marker and shaded
 * phase sits along the track (as fractions 0..1), which commits cluster,
 * which markers stack into lanes, the end labels and the axis ticks. It is
 * unit-tested with `node --test` (tests/js/pr-timeline.test.mjs). `render`
 * turns a layout into DOM; the page script (stack-table.js) mounts it into
 * each card and keeps the per-card expanded / hide-commits state.
 *
 * Track data (the card's data-track attribute, from the server):
 *   {created, ended, state, draft, events: [{t, kind, actor, detail, ci,
 *    subject}]}
 * with times as ISO strings, `ended` the merge/close time (or null), `state`
 * the PR state and `draft` whether it is a draft right now.
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

  const KIND_LABELS = {
    opened: "opened",
    commit: "commit",
    commits: "commits",
    commit_failed: "commit (CI failed)",
    force_push: "force push",
    review_requested: "review requested",
    review_request_removed: "request removed",
    ready_for_review: "ready for review",
    converted_to_draft: "back to draft",
    review_approved: "approved",
    review_changes: "changes requested",
    review_commented: "commented",
    merged: "merged",
    closed: "closed",
    reopened: "reopened",
  };

  const PHASE_LABELS = {
    draft: "In draft",
    waiting: "Waiting for review",
    ci_failing: "CI failing",
  };

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
  // reviewer, or with failing CI, as [{kind, t0, t1}] in ms.
  //
  // - draft: from creation (when the first draft toggle is "ready for
  //   review", or the PR is a draft now and was never toggled) or each
  //   "back to draft", until "ready for review".
  // - waiting: not a draft and at least one review request outstanding (a
  //   review by the reviewer, or removing the request, settles it).
  // - ci_failing: from a commit whose CI failed until the next commit or
  //   force push (which supersedes it), or the end of the PR.
  function phaseSpans(events, track, start, end) {
    const toggles = events.filter(
      (e) => e.kind === "ready_for_review" || e.kind === "converted_to_draft",
    );
    let draft = toggles.length ? toggles[0].kind === "ready_for_review" : Boolean(track.draft);
    const pending = new Set();
    let failing = false;
    const open = { draft: draft ? start : null, waiting: null, ci_failing: null };
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
      else if (event.kind === "review") pending.delete(who);
      else if (event.kind === "commit") failing = event.ci === "failed";
      else if (event.kind === "force_push") failing = false;
      else continue;
      set("draft", draft, event.ms);
      set("waiting", pending.size > 0 && !draft, event.ms);
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
   *   now        current time (ms), required
   *   commits    "none" (no commits at all), "significant" (only commits
   *              whose CI failed) or "all" (ordinary commits as faint dots,
   *              clustered where they would overlap)
   *   minGap     fraction of the track under which commits cluster (0.012)
   *   laneGap    fraction of the track two prominent markers need between
   *              them to share a lane (more when labels are drawn)
   *   maxLanes   most lanes markers stack into (3)
   */
  function trackLayout(track, options) {
    const now = options.now;
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
        items: [{ t: event.t, actor: event.actor, detail: event.detail }],
      };
      marker.title = `${KIND_LABELS[kind]}${event.detail && kind !== "opened" && event.kind !== "review" ? `: ${event.detail}` : ""}` +
        `${event.actor ? ` (${event.actor})` : ""} ${age(event.ms)}`;
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
      return {
        ...first,
        kind: "commits",
        x,
        count: members.length,
        detail: `${members.length} commits`,
        items: members.flatMap((m) => m.items),
        title: `${members.length} commits, ${age(first.ms)} to ${age(members[members.length - 1].ms)}`,
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

    const phases = phaseSpans(events, track, start, end).map((p) => ({
      kind: p.kind,
      label: PHASE_LABELS[p.kind],
      x0: xOf(p.t0),
      x1: xOf(p.t1),
      t0: new Date(p.t0).toISOString(),
      t1: new Date(p.t1).toISOString(),
      title: `${PHASE_LABELS[p.kind]} for ${formatAge((p.t1 - p.t0) / 1000)}`,
    }));

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

  // The plain chronological list: every event (commits unless "none"),
  // oldest first.
  function listEntries(track, options) {
    const now = options.now;
    const hideCommits = options.commits === "none";
    return (track.events || [])
      .map((event, seq) => ({ event, seq, ms: Date.parse(event.t) }))
      .sort((a, b) => a.ms - b.ms || a.seq - b.seq)
      .filter(({ event }) => !(hideCommits && event.kind === "commit"))
      .map(({ event, ms }) => {
        const kind = markerKind(event);
        return {
          t: event.t,
          kind,
          label: KIND_LABELS[kind],
          actor: event.actor,
          detail: event.detail,
          ci: event.ci || "",
          age: `${formatAge((now - ms) / 1000)} ago`,
        };
      });
  }

  // ---- DOM ----------------------------------------------------------------

  const GLYPHS = {
    opened: "◆",
    commit: "•",
    commits: "",
    commit_failed: "●",
    force_push: "↯",
    review_requested: "▸",
    review_request_removed: "⊘",
    ready_for_review: "▶",
    converted_to_draft: "◐",
    review_approved: "✓",
    review_changes: "✗",
    review_commented: "✎",
    merged: "⬤",
    closed: "✕",
    reopened: "↺",
  };

  function el(document, tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /*
   * Builds the DOM for one card's timeline. `options`: now (ms), expanded,
   * hideCommits. Collapsed: a slim track with the prominent events. Expanded:
   * a taller track with labelled events, faint commit dots (unless
   * hideCommits) and the chronological list.
   */
  function render(document, track, options) {
    const { now, expanded, hideCommits } = options;
    const commits = hideCommits ? "none" : expanded ? "all" : "significant";
    const layout = trackLayout(track, {
      now,
      commits,
      laneGap: expanded ? 0.11 : 0.015,
      maxLanes: expanded ? 4 : 2,
    });
    const root = el(document, "div", `tl ${expanded ? "tl-expanded" : "tl-collapsed"}`);
    const laneStep = expanded ? 20 : 8;
    const base = expanded ? 34 : 24;
    const height = base + (layout.lanes - 1) * laneStep;
    root.style.setProperty("--tl-height", `${height}px`);

    const head = el(document, "div", "tl-ends");
    const buttons = el(document, "span", "tl-buttons");
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
      const node = el(document, "span", `tl-phase tl-phase-${phase.kind}`);
      node.style.left = `${phase.x0 * 100}%`;
      node.style.width = `${Math.max((phase.x1 - phase.x0) * 100, 0.4)}%`;
      node.title = phase.title;
      trackEl.append(node);
    }
    for (const marker of layout.markers) {
      const faint = marker.kind === "commit" || marker.kind === "commits";
      const node = el(
        document,
        "span",
        `tl-mark tl-k-${marker.kind}${faint ? " tl-faint" : ""}`,
        marker.kind === "commits" ? String(marker.count) : GLYPHS[marker.kind],
      );
      node.style.left = `${marker.x * 100}%`;
      node.title = marker.title;
      if (!faint) {
        node.style.top = `${(expanded ? 14 : 12) + marker.lane * laneStep}px`;
      }
      if (expanded && !faint) {
        const label = el(
          document,
          "span",
          `tl-label${marker.x > 0.78 ? " tl-flip" : ""}`,
          marker.actor && marker.kind.startsWith("review")
            ? `${KIND_LABELS[marker.kind]} · ${marker.actor}`
            : KIND_LABELS[marker.kind],
        );
        node.append(label);
      }
      trackEl.append(node);
    }
    if (layout.now !== null) trackEl.append(el(document, "span", "tl-now"));
    trackEl.style.height = `${height}px`;
    root.append(head, trackEl);

    if (expanded) {
      const list = el(document, "ol", "tl-list");
      for (const entry of listEntries(track, { now, commits })) {
        const li = el(document, "li", `tl-item tl-i-${entry.kind}`);
        const when = el(document, "time", "tl-when", entry.age);
        when.title = new Date(entry.t).toLocaleString();
        li.append(
          when,
          el(document, "span", "tl-what", entry.label + (entry.ci === "failed" ? " · CI failed" : "")),
          el(document, "span", "tl-who", entry.actor),
          el(document, "span", "tl-detail", entry.detail),
        );
        list.append(li);
      }
      root.append(list);
    }
    return root;
  }

  return {
    KIND_LABELS,
    PHASE_LABELS,
    formatAge,
    markerKind,
    phaseSpans,
    axisTicks,
    trackLayout,
    listEntries,
    render,
  };
});
