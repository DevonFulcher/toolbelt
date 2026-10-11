import type { CSSProperties } from "react";
import { serializeState } from "../logic/viewState";
import { useApp, useAppStore } from "../store/StoreContext";

/**
 * What stands in for the branches below the depth cap: "continue from here"
 * re-roots the view at this branch, so a stack of any length can be walked.
 */
export function CapControl({
  id,
  depth,
  hidden,
}: {
  id: string;
  depth: number;
  hidden: { levels: number; branches: number };
}) {
  const store = useAppStore();
  const href = useApp((s) => serializeState({ ...s.view, root: id }));
  const { levels, branches } = hidden;
  return (
    <div className="cap" style={{ "--depth": depth } as CSSProperties}>
      <a
        className="cap-continue"
        href={href}
        onClick={(event) => {
          event.preventDefault();
          store.getState().focusRoot(id);
        }}
      >
        {`${levels} more ${levels === 1 ? "level" : "levels"} below — continue from here →`}
      </a>
      <span className="cap-count">{`${branches} ${branches === 1 ? "branch" : "branches"} not shown`}</span>
    </div>
  );
}
