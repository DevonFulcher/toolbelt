import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { knownRoot } from "../logic/focus";
import { groupedLayout, railGuides, stackedLayout, type Section } from "../logic/layout";
import type { Row } from "../logic/rows";
import { useApp, useAppStore } from "../store/StoreContext";
import { Card, Stub } from "./Card";
import { CapControl } from "./CapControl";

const NO_RAILS: never[] = [];

/**
 * The card views: `stacked` (one section per repo, indented by stack depth
 * with rails to the parents) and `grouped` (sections by what happens next).
 * Sections are collapsible with sticky headers; a card whose section changed
 * since the last layout (its next action moved it) flashes.
 */
export function CardList({ rows }: { rows: Row[] }) {
  const store = useAppStore();
  const view = useApp((s) => s.view);
  const collapsed = useApp((s) => s.collapsed);
  const stacked = view.view === "stacked";
  // The branch the view is focused on (a root that is no branch is ignored).
  const focusRoot = stacked ? knownRoot(rows, view.root) : "";
  const byId = useMemo(() => new Map(rows.map((row) => [row.id, row])), [rows]);
  const sections = useMemo(
    () => (stacked ? stackedLayout(rows, view) : groupedLayout(rows, view)),
    [rows, view, stacked],
  );

  // Where each card is placed (section key), and which sections hold a stub
  // for which branch.
  const placement = useMemo(() => {
    const placed = new Map<string, Section>();
    const stubs = new Set<string>();
    for (const section of sections) {
      for (const entry of section.entries) {
        if (entry.context) stubs.add(`${section.key}|${entry.id}`);
        else placed.set(entry.id, section);
      }
    }
    return { placed, stubs };
  }, [sections]);

  // A card that was in another section last time moved because its next
  // action did. (A different view starts afresh.)
  const lastSections = useRef<{ view: string; keys: Map<string, string> }>({
    view: view.view,
    keys: new Map(),
  });
  const moved = useMemo(() => {
    const out = new Set<string>();
    const last = lastSections.current;
    if (last.view !== view.view) return out;
    for (const [id, section] of placement.placed) {
      const before = last.keys.get(id);
      if (before !== undefined && before !== section.key) out.add(id);
    }
    return out;
    // `lastSections` is read-only here; it is updated after the commit.
  }, [placement, view.view]);
  useEffect(() => {
    lastSections.current = {
      view: view.view,
      keys: new Map([...placement.placed].map(([id, section]) => [id, section.key])),
    };
  }, [placement, view.view]);

  // Scrolling to a related card (or the stub standing for it in the same
  // section), opening its section if collapsed, and highlighting it.
  const [jump, setJump] = useState<{ id: string; section: string; n: number } | null>(null);
  // The handler is stable (the cards are memoised on it); it reads the
  // placement of the latest layout.
  const placementRef = useRef(placement);
  useEffect(() => {
    placementRef.current = placement;
  }, [placement]);
  const onJump = useCallback(
    (target: string, from: string) => {
      // The focus root's parent is not in the view: go there instead.
      if (from === store.getState().view.root) {
        store.getState().focusRoot(target);
        return;
      }
      const { placed, stubs } = placementRef.current;
      const own = placed.get(target);
      const fromSection = placed.get(from);
      const sectionKey =
        own?.key ?? (fromSection && stubs.has(`${fromSection.key}|${target}`) ? fromSection.key : null);
      if (!sectionKey) return;
      store.getState().expandSection(sectionKey);
      setJump((previous) => ({ id: target, section: sectionKey, n: (previous?.n ?? 0) + 1 }));
    },
    [store],
  );
  // A leaf card's "open in stacked view": once the stacked layout is there,
  // scroll to the card and highlight it among its stack.
  const reveal = useRef<string | null>(null);
  const onOpenInStack = useCallback(
    (id: string, root: string) => {
      reveal.current = id;
      store.getState().focusRoot(root, "stacked");
    },
    [store],
  );
  useEffect(() => {
    const id = reveal.current;
    const section = id ? placement.placed.get(id) : undefined;
    if (!id || !stacked || !section) return;
    reveal.current = null;
    store.getState().expandSection(section.key);
    setJump((previous) => ({ id, section: section.key, n: (previous?.n ?? 0) + 1 }));
  }, [placement, stacked, store]);
  useEffect(() => {
    if (!jump) return undefined;
    const section = [...document.querySelectorAll<HTMLElement>(".cards-section")].find(
      (el) => el.dataset.key === jump.section,
    );
    const target = [...(section?.querySelectorAll<HTMLElement>("[data-id]") ?? [])].find(
      (el) => el.dataset.id === jump.id,
    );
    target?.scrollIntoView?.({ block: "center", behavior: "smooth" });
    const timer = window.setTimeout(() => setJump(null), 1800);
    return () => window.clearTimeout(timer);
  }, [jump]);

  return (
    <div className="cards-wrap" id="cards-wrap">
      <div className={`cards${stacked ? " cards-stacked" : ""}`} id="cards">
        {sections.map((section) => {
          const isCollapsed = collapsed.includes(section.key);
          const guides = stacked ? railGuides(section.entries) : [];
          const count = section.entries.filter((entry) => !entry.context).length;
          return (
            <section
              key={section.key}
              className={`cards-section${isCollapsed ? " collapsed" : ""}`}
              data-key={section.key}
              data-tone={section.tone || undefined}
            >
              <h2 className="cards-group">
                <button
                  type="button"
                  aria-expanded={!isCollapsed}
                  onClick={() => store.getState().toggleSection(section.key)}
                >
                  <span className="cg-caret" aria-hidden="true" />
                  <span className="cg-label">{section.label}</span>
                  <span className="cg-count">{count}</span>
                  {section.meaning && <span className="cg-meaning">{section.meaning}</span>}
                </button>
              </h2>
              {!isCollapsed && (
                <div className="cards-section-body">
                  {section.entries.map((entry, index) => {
                    const rails = stacked ? guides[index] : NO_RAILS;
                    const depth = stacked ? entry.depth : 0;
                    if (entry.context) {
                      return (
                        <Stub
                          key={`stub:${entry.id}`}
                          id={entry.id}
                          depth={depth}
                          rails={rails}
                          highlighted={jump?.id === entry.id && jump.section === section.key}
                        />
                      );
                    }
                    const row = byId.get(entry.id);
                    const parentId = row?.parent ?? null;
                    const parentSection = parentId ? placement.placed.get(parentId) : undefined;
                    const parentStub = parentId
                      ? placement.stubs.has(`${section.key}|${parentId}`)
                      : false;
                    const outside = Boolean(parentId) && entry.id === focusRoot;
                    return (
                      <Fragment key={entry.id}>
                        <Card
                          id={entry.id}
                          depth={depth}
                          rails={rails}
                          moved={moved.has(entry.id)}
                          highlighted={jump?.id === entry.id}
                          parentIn={
                            parentSection && parentSection.key !== section.key ? parentSection.label : null
                          }
                          parentGone={Boolean(parentId) && !parentSection && !parentStub && !outside}
                          parentOutside={outside}
                          onJump={onJump}
                          onOpenInStack={onOpenInStack}
                        />
                        {entry.hidden && (
                          <CapControl id={entry.id} depth={depth} hidden={entry.hidden} />
                        )}
                      </Fragment>
                    );
                  })}
                </div>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}
