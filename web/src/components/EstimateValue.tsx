import type { BranchExtras } from "../api/generated";
import { estimateText } from "../logic/finishEstimate";
import { browserTimeZone } from "../logic/localTime";

/** A task's estimated finish: the median and p80 from now, the explanation and
 * the instants in the tooltip. */
export function EstimateValue({ extras, now }: { extras: BranchExtras | undefined; now: number }) {
  if (extras === undefined) return <>loading estimate…</>;
  if (!extras.estimate) return <>–</>;
  const text = estimateText(extras.estimate, now, browserTimeZone());
  return (
    <span className={text.known ? "estimate" : "muted estimate"} title={text.title}>
      {text.text}
    </span>
  );
}
