import { useCallback, useState } from "react";
import type { BranchExtras } from "../api/generated";
import { estimateText } from "../logic/finishEstimate";
import { browserTimeZone } from "../logic/localTime";
import { EstimateDialog } from "./EstimateDialog";

/** A task's estimated finish: the champion model's median and p80 from now,
 * the explanation and the instants in the tooltip, and a button that opens
 * every model's answer. */
export function EstimateValue({
  extras,
  now,
  branch,
}: {
  extras: BranchExtras | undefined;
  now: number;
  branch: string;
}) {
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  if (extras === undefined) return <>loading estimate…</>;
  if (!extras.estimate) return <>–</>;
  const text = estimateText(extras.estimate, now, browserTimeZone());
  return (
    <>
      <span className={text.known ? "estimate" : "muted estimate"} title={text.title}>
        {text.text}
      </span>{" "}
      <button type="button" className="agents-toggle" aria-haspopup="dialog" onClick={() => setOpen(true)}>
        Models
      </button>
      {open && <EstimateDialog id={extras.id} branch={branch} now={now} onClose={close} />}
    </>
  );
}
