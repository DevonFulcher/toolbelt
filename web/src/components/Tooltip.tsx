import {
  createContext,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
  type SyntheticEvent,
} from "react";
import { positionTip, tooltipContent, type Tip } from "../logic/timeline";
import { useNow } from "./useNow";

export interface TooltipApi {
  show(tip: Tip, anchor: HTMLElement): void;
  hide(): void;
}

const TooltipContext = createContext<TooltipApi>({ show: () => {}, hide: () => {} });

export function useTooltip(): TooltipApi {
  return useContext(TooltipContext);
}

interface Shown {
  tip: Tip;
  anchor: HTMLElement;
}

const TIP_ID = "tl-tip";

/**
 * One tooltip for the whole page: shown immediately when a marker, cluster or
 * span is hovered or focused, hidden on leave/blur/Escape or when its anchor
 * goes away, kept inside the viewport (and following the anchor on scroll).
 */
export function TooltipProvider({ children }: { children: ReactNode }) {
  const [shown, setShown] = useState<Shown | null>(null);
  const api = useMemo<TooltipApi>(
    () => ({
      show: (tip, anchor) => setShown({ tip, anchor }),
      hide: () => setShown(null),
    }),
    [],
  );
  return (
    <TooltipContext.Provider value={api}>
      {children}
      {shown && <TooltipBox shown={shown} onHide={api.hide} />}
    </TooltipContext.Provider>
  );
}

function TooltipBox({ shown, onHide }: { shown: Shown; onHide: () => void }) {
  const box = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState<{ left: number; top: number; placement: string } | null>(null);
  const [scrolls, setScrolls] = useState(0);
  const now = useNow();
  const content = useMemo(() => tooltipContent(shown.tip, now), [shown.tip, now]);

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    if (!shown.anchor.isConnected) {
      onHide();
      return;
    }
    const rect = shown.anchor.getBoundingClientRect();
    const size = el.getBoundingClientRect();
    setPlace(
      positionTip(
        rect,
        { width: size.width, height: size.height },
        { width: window.innerWidth, height: window.innerHeight },
      ),
    );
  }, [shown, scrolls, onHide]);

  useEffect(() => {
    const { anchor } = shown;
    anchor.setAttribute("aria-describedby", TIP_ID);
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "Escape") onHide();
    };
    // Scrolling (also what keyboard focus does to bring a marker into view)
    // moves the anchor: follow it.
    const onScroll = (): void => setScrolls((n) => n + 1);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      anchor.removeAttribute("aria-describedby");
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [shown, onHide]);

  return (
    <div
      ref={box}
      id={TIP_ID}
      role="tooltip"
      className="tl-tip"
      data-placement={place?.placement}
      style={{ left: place?.left ?? 0, top: place?.top ?? 0, visibility: place ? "visible" : "hidden" }}
    >
      <div className="tl-tip-title">{content.title}</div>
      {content.subtitle && <div className="tl-tip-sub">{content.subtitle}</div>}
      {content.rows.map(([key, value]) => (
        <div className="tl-tip-row" key={key}>
          <span className="tl-tip-key">{key}</span>
          <span className="tl-tip-val">{value}</span>
        </div>
      ))}
      {content.items.length > 0 && (
        <ul className="tl-tip-items">
          {content.items.map((text, index) => (
            <li key={index}>{text}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * A focusable element that shows `tip` on hover and on keyboard focus;
 * `label` is its accessible name.
 */
export function TipSource({
  tip,
  label,
  className,
  style,
  children,
}: {
  tip: Tip;
  label: string;
  className: string;
  style?: CSSProperties;
  children?: ReactNode;
}) {
  const { show, hide } = useTooltip();
  const onShow = (event: SyntheticEvent<HTMLElement>): void => show(tip, event.currentTarget);
  return (
    <span
      role="img"
      aria-label={label}
      tabIndex={0}
      className={className}
      style={style}
      onMouseEnter={onShow}
      onFocus={onShow}
      onMouseLeave={hide}
      onBlur={hide}
    >
      {children}
    </span>
  );
}
