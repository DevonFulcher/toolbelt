import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { groupedLayout, prioritiesLayout, railGuides, stackedLayout, type Section } from "../logic/layout";
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
  const folded = useApp((s) => s.folded);
  const stacked = view.view === "stacked";
  const sections = useMemo(
    () =>
      stacked
        ? stackedLayout(rows, view, new Set(folded))
        : view.view === "priorities"
          ? prioritiesLayout(rows, view)
          : groupedLayout(rows, view),
    [rows, view, stacked, folded],
  );

  // Where each card is placed (section key).
  const placement = useMemo(() => {
    const placed = new Map<string, Section>();
    for (const section of sections) {
      for (const entry of section.entries) {
        if (!entry.context) placed.set(entry.id, section);
      }
    }
    return { placed };
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

  // Highlighting a card opened in its stack.
  const [jump, setJump] = useState<{ id: string; section: string; n: number } | null>(null);
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
                    return (
                      <Fragment key={entry.id}>
                        <Card
                          id={entry.id}
                          depth={depth}
                          rails={rails}
                          moved={moved.has(entry.id)}
                          highlighted={jump?.id === entry.id}
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
