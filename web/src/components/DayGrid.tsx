import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { dayGrid, gridDensity, visibleDays } from "../logic/dayGrid";

/** Width assumed where nothing can be measured (a zero-width layout). */
const UNMEASURED_PX = 600;

/**
 * The calendar decoration behind a track's phases and markers: a band per
 * weekend and a line at each local midnight, labelled with its weekday when
 * the track is expanded. It fills the track, ignores the pointer and is
 * hidden from assistive tech (the timeline's list has the real times).
 */
export function DayGrid({ start, end, labelled }: { start: number; end: number; labelled: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setWidth(el.clientWidth);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const grid = useMemo(() => dayGrid(start, end), [start, end]);
  const density = gridDensity(width || UNMEASURED_PX, end - start);
  const days = visibleDays(grid, density);
  return (
    <div className="tl-daygrid" ref={ref} aria-hidden="true">
      {grid.weekends.map((weekend) => (
        <div
          key={weekend.t0}
          className="tl-weekend"
          style={{ left: `${weekend.x0 * 100}%`, width: `${(weekend.x1 - weekend.x0) * 100}%` }}
        />
      ))}
      {days.map((day) => (
        <div
          key={day.t}
          className={`tl-day${day.weekday === 1 ? " tl-day-week" : ""}`}
          data-weekday={day.label}
          style={{ left: `${day.x * 100}%` }}
        >
          {labelled && (
            <span className="tl-day-label">{density.letters === 3 ? day.label : day.label[0]}</span>
          )}
        </div>
      ))}
    </div>
  );
}
