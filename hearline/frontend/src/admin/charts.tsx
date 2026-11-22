// Small charts for the admin dashboard, in plain HTML/CSS (no chart library).
// Colors are CSS variables in index.css (--series-1, --good, --critical), with dark-mode steps.
import { useState, type ReactNode } from "react";

export interface Series {
  key: string;
  label: string;
  color: string; // a CSS color, e.g. "var(--series-1)"
  values: Record<string, number>; // day -> value
}

interface DailyProps {
  days: string[]; // "YYYY-MM-DD", oldest first
  series: Series[]; // stacked bottom-up in this order
  format: (value: number) => string;
  formatDay: (day: string) => string;
  emptyText: string;
}

/** Daily bars (stacked when there are several series), with a per-day hover readout. */
export function DailyBars({ days, series, format, formatDay, emptyText }: DailyProps) {
  const [hover, setHover] = useState<string | null>(null);
  const total = (day: string) => series.reduce((sum, s) => sum + (s.values[day] ?? 0), 0);
  const max = Math.max(0, ...days.map(total));
  if (max === 0) return <p className="flex h-40 items-center justify-center text-sm text-neutral-400">{emptyText}</p>;
  const ticks = [days[0], days[Math.floor((days.length - 1) / 2)], days[days.length - 1]];

  return (
    <div>
      {series.length > 1 && (
        <div className="mb-3 flex flex-wrap gap-4 text-xs text-neutral-500">
          {series.map((s) => (
            <span key={s.key} className="flex items-center gap-1.5">
              <span className="h-2.5 w-2.5 rounded-sm" style={{ background: s.color }} />
              {s.label}
            </span>
          ))}
        </div>
      )}
      <div className="relative">
        <span className="absolute -top-1 left-0 text-[11px] text-neutral-400">{format(max)}</span>
        <div
          className="flex h-40 items-end gap-[2px] border-b border-neutral-200 pt-4 dark:border-neutral-700"
          onPointerLeave={() => setHover(null)}
        >
          {days.map((day) => (
            <div
              key={day}
              tabIndex={0}
              onPointerEnter={() => setHover(day)}
              onFocus={() => setHover(day)}
              onBlur={() => setHover(null)}
              aria-label={`${formatDay(day)}: ${series.map((s) => `${s.label} ${format(s.values[day] ?? 0)}`).join(", ")}`}
              className="relative flex h-full flex-1 flex-col-reverse gap-[2px] outline-none"
            >
              {series.map((s) => {
                const v = s.values[day] ?? 0;
                return v > 0 ? (
                  <div
                    key={s.key}
                    className="w-full last:rounded-t-[4px]"
                    style={{
                      height: `${(v / max) * 100}%`,
                      background: s.color,
                      opacity: hover && hover !== day ? 0.55 : 1,
                    }}
                  />
                ) : null;
              })}
              {hover === day && (
                <div className="pointer-events-none absolute bottom-full left-1/2 z-10 mb-2 -translate-x-1/2 whitespace-nowrap rounded-lg border border-neutral-200 bg-white px-3 py-2 text-xs shadow-md dark:border-neutral-700 dark:bg-neutral-900">
                  <p className="mb-1 text-neutral-500">{formatDay(day)}</p>
                  {series.map((s) => (
                    <p key={s.key} className="flex items-center gap-2">
                      <span className="h-0.5 w-3" style={{ background: s.color }} />
                      <strong className="font-semibold">{format(s.values[day] ?? 0)}</strong>
                      {series.length > 1 && <span className="text-neutral-500">{s.label}</span>}
                    </p>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
        <div className="mt-1 flex justify-between text-[11px] text-neutral-400">
          {ticks.map((d, i) => (
            <span key={i}>{formatDay(d)}</span>
          ))}
        </div>
      </div>
    </div>
  );
}

interface RankedProps {
  rows: { label: string; value: number; extra?: ReactNode }[]; // largest first; extra = e.g. a delta
  format: (value: number) => string;
  emptyText: string;
}

/** Horizontal bars for a ranking (top topics, cost by stage). One hue; values are labeled. */
export function RankedBars({ rows, format, emptyText }: RankedProps) {
  const max = Math.max(0, ...rows.map((r) => r.value));
  if (max === 0) return <p className="flex h-24 items-center justify-center text-sm text-neutral-400">{emptyText}</p>;
  return (
    <ul className="space-y-2.5">
      {rows.map((r) => (
        <li key={r.label} className="grid grid-cols-[minmax(0,9rem)_1fr_auto_auto] items-center gap-3 text-sm">
          <span className="truncate text-neutral-600 dark:text-neutral-300" title={r.label}>
            {r.label}
          </span>
          <span className="h-3 rounded-r-[4px] bg-[var(--series-1)]" style={{ width: `${Math.max(2, (r.value / max) * 100)}%` }} />
          <span className="tabular-nums font-medium">{format(r.value)}</span>
          <span className="min-w-[4.5rem] text-right">{r.extra}</span>
        </li>
      ))}
    </ul>
  );
}

/** The same numbers as a table (the "Table" view of every chart). */
export function DataTable({ columns, rows }: { columns: string[]; rows: ReactNode[][] }) {
  return (
    <div className="max-h-72 overflow-auto rounded-lg border border-neutral-200 dark:border-neutral-800">
      <table className="w-full text-sm">
        <thead className="sticky top-0 bg-neutral-50 text-left text-xs text-neutral-500 dark:bg-neutral-800">
          <tr>
            {columns.map((c, i) => (
              <th key={c} className={`px-3 py-2 font-medium ${i > 0 ? "text-right" : ""}`}>
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-neutral-100 dark:divide-neutral-800">
          {rows.map((row, r) => (
            <tr key={r}>
              {row.map((cell, i) => (
                <td key={i} className={`px-3 py-1.5 ${i > 0 ? "text-right tabular-nums" : ""}`}>
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
