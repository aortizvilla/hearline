// Trend visuals for the admin dashboard, drawn in SVG at the container's real width:
// - LineChart: the current period (solid) over the previous one (dashed, same day number),
//   today's point highlighted, a crosshair + tooltip on hover/focus
// - Sparkline: the same, small, for KPI cards
// - Bullet: a rate (0-1) as a bar, with a tick where it was in the previous period
import { useEffect, useRef, useState } from "react";

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!ref.current) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

const PREVIOUS = "var(--previous)";

function path(values: number[], x: (i: number) => number, y: (v: number) => number): string {
  return values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
}

interface LineProps {
  days: string[]; // current period, oldest first; the last one is today
  current: number[];
  previous?: number[]; // same length: day i of the previous period
  previousDays?: string[];
  format: (v: number) => string;
  formatDay: (d: string) => string;
  labels: { current: string; previous?: string; today: string };
  height?: number;
  highlightLast?: boolean; // mark the last point as "today"
}

export function LineChart({ days, current, previous, previousDays, format, formatDay, labels, height = 200, highlightLast = true }: LineProps) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const pad = { top: 16, right: 12, bottom: 22, left: 44 };
  const max = Math.max(1e-9, ...current, ...(previous ?? []));
  const w = Math.max(0, width - pad.left - pad.right);
  const h = height - pad.top - pad.bottom;
  const x = (i: number) => pad.left + (days.length > 1 ? (i / (days.length - 1)) * w : w / 2);
  const y = (v: number) => pad.top + h - (v / max) * h;
  const last = current.length - 1;
  const ticks = [0, max / 2, max];

  const pick = (clientX: number, el: SVGSVGElement) => {
    const rel = clientX - el.getBoundingClientRect().left - pad.left;
    setHover(Math.max(0, Math.min(last, Math.round((rel / (w || 1)) * last))));
  };

  return (
    <div ref={ref} className="relative">
      <div className="mb-2 flex flex-wrap gap-4 text-xs text-neutral-500">
        <span className="flex items-center gap-1.5">
          <span className="h-0.5 w-4 rounded bg-[var(--series-1)]" /> {labels.current}
        </span>
        {previous && labels.previous && (
          <span className="flex items-center gap-1.5">
            <span className="w-4 border-t-2 border-dashed border-[var(--previous)]" /> {labels.previous}
          </span>
        )}
        {highlightLast && (
          <span className="flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-full bg-[var(--series-1)] ring-2 ring-[var(--series-1)]/30" /> {labels.today}
          </span>
        )}
      </div>
      {width > 0 && (
        <svg
          width={width}
          height={height}
          className="block touch-none outline-none"
          tabIndex={0}
          role="img"
          aria-label={`${labels.current}: ${format(current[last] ?? 0)}`}
          onPointerMove={(e) => pick(e.clientX, e.currentTarget)}
          onPointerLeave={() => setHover(null)}
          onFocus={() => setHover(last)}
          onBlur={() => setHover(null)}
          onKeyDown={(e) => {
            if (e.key === "ArrowLeft") setHover((i) => Math.max(0, (i ?? last) - 1));
            if (e.key === "ArrowRight") setHover((i) => Math.min(last, (i ?? last) + 1));
          }}
        >
          {ticks.map((t, i) => (
            <g key={i}>
              <line x1={pad.left} x2={pad.left + w} y1={y(t)} y2={y(t)} className="stroke-neutral-200 dark:stroke-neutral-800" strokeWidth={1} />
              <text x={pad.left - 6} y={y(t) + 4} textAnchor="end" className="fill-neutral-400 text-[11px]">
                {format(t)}
              </text>
            </g>
          ))}
          {[0, Math.floor(last / 2), last].map((i) => (
            <text key={i} x={x(i)} y={height - 4} textAnchor={i === 0 ? "start" : i === last ? "end" : "middle"} className="fill-neutral-400 text-[11px]">
              {formatDay(days[i])}
            </text>
          ))}
          {previous && (
            <path d={path(previous, x, y)} fill="none" stroke={PREVIOUS} strokeWidth={2} strokeDasharray="5 4" strokeLinejoin="round" />
          )}
          <path d={`${path(current, x, y)}L${x(last)},${y(0)}L${x(0)},${y(0)}Z`} fill="var(--series-1)" opacity={0.08} />
          <path d={path(current, x, y)} fill="none" stroke="var(--series-1)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
          {highlightLast && last >= 0 && (
            <g>
              <circle cx={x(last)} cy={y(current[last])} r={9} fill="var(--series-1)" opacity={0.2} />
              <circle cx={x(last)} cy={y(current[last])} r={5} fill="var(--series-1)" className="stroke-white dark:stroke-neutral-900" strokeWidth={2} />
            </g>
          )}
          {hover !== null && (
            <g>
              <line x1={x(hover)} x2={x(hover)} y1={pad.top} y2={pad.top + h} className="stroke-neutral-400" strokeWidth={1} />
              <circle cx={x(hover)} cy={y(current[hover])} r={4} fill="var(--series-1)" />
              {previous && <circle cx={x(hover)} cy={y(previous[hover])} r={4} fill={PREVIOUS} />}
            </g>
          )}
        </svg>
      )}
      {hover !== null && width > 0 && (
        <div
          className="pointer-events-none absolute z-10 rounded-lg border border-neutral-200 bg-white px-3 py-2 text-xs whitespace-nowrap shadow-md dark:border-neutral-700 dark:bg-neutral-900"
          style={{ left: Math.min(Math.max(x(hover) - 70, 0), width - 160), top: 24 }}
        >
          <p className="flex items-center gap-2">
            <span className="h-0.5 w-3 bg-[var(--series-1)]" />
            <strong className="font-semibold">{format(current[hover])}</strong>
            <span className="text-neutral-500">{hover === last && highlightLast ? labels.today : formatDay(days[hover])}</span>
          </p>
          {previous && (
            <p className="mt-1 flex items-center gap-2">
              <span className="w-3 border-t-2 border-dashed border-[var(--previous)]" />
              <strong className="font-semibold">{format(previous[hover])}</strong>
              {previousDays && <span className="text-neutral-500">{formatDay(previousDays[hover])}</span>}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

/** A small trend line for a KPI card: current (solid) over previous (dashed), last point marked. */
export function Sparkline({ current, previous, height = 40 }: { current: number[]; previous?: number[]; height?: number }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const max = Math.max(1e-9, ...current, ...(previous ?? []));
  const last = current.length - 1;
  const x = (i: number) => 3 + (last > 0 ? (i / last) * (width - 6) : 0);
  const y = (v: number) => 3 + (height - 6) * (1 - v / max);
  return (
    <div ref={ref} aria-hidden>
      {width > 0 && last >= 0 && (
        <svg width={width} height={height} className="block">
          {previous && <path d={path(previous, x, y)} fill="none" stroke={PREVIOUS} strokeWidth={1.5} strokeDasharray="3 3" />}
          <path d={path(current, x, y)} fill="none" stroke="var(--series-1)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
          <circle cx={x(last)} cy={y(current[last])} r={3.5} fill="var(--series-1)" />
        </svg>
      )}
    </div>
  );
}

/** A rate (0-1) as a filled bar, with a tick where the previous period was. */
export function Bullet({ value, previous, label }: { value: number | null; previous: number | null; label: string }) {
  const pct = (v: number) => `${Math.max(0, Math.min(1, v)) * 100}%`;
  return (
    <div className="relative mt-3 h-2.5 rounded-full bg-neutral-100 dark:bg-neutral-800" role="img" aria-label={label}>
      {value !== null && <div className="h-full rounded-full bg-[var(--series-1)]" style={{ width: pct(value) }} />}
      {previous !== null && (
        <div className="absolute -top-1 h-[18px] w-0.5 rounded bg-neutral-700 dark:bg-neutral-200" style={{ left: `calc(${pct(previous)} - 1px)` }} />
      )}
    </div>
  );
}
