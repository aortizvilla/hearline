// Admin dashboard, built to show whether the app is improving:
// 1. filters (one row)  2. insights found in the data  3. engaged users and new users as line
// charts (current period solid, previous dashed, today highlighted)  4. KPI cards with a trend
// line or a bar vs last period  5. retention and engagement  6. operations (health, volume, cost).
// Every number shows its change vs the previous period (month over month with 30 days).
import { useEffect, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";

import {
  api,
  type DailySeries,
  type Dashboard,
  type DashboardFilters,
  type HealthStatus,
  type PeriodKpis,
} from "../api";
import { DailyBars, DataTable, RankedBars } from "./charts";
import { Bullet, LineChart, Sparkline } from "./trend";
import { useI18n } from "../i18n";

const PERIODS = [7, 30, 90];
const NO_FILTERS: DashboardFilters = { days: 30, language: "", voice: "", topic: "" };
const card = "rounded-2xl border border-neutral-200 bg-white p-5 shadow-sm dark:border-neutral-800 dark:bg-neutral-900";
const select =
  "rounded-xl border border-neutral-200 bg-white px-3 py-2 text-sm outline-none focus:border-brand-500 dark:border-neutral-700 dark:bg-neutral-800";
type Tab = "audience" | "pipeline";
const STATUS_ICON: Record<HealthStatus, string> = { good: "✓", warning: "!", critical: "✕" };

type Kind = "count" | "rate" | "money" | "number" | "seconds";
type KpiKey = { [K in keyof PeriodKpis]: PeriodKpis[K] extends number | null ? K : never }[keyof PeriodKpis];
type SeriesKey = Exclude<keyof DailySeries, "days" | "cost_per_episode" | "generation_median">;
interface Kpi {
  key: KpiKey;
  label: string;
  kind: Kind;
  lowerIsBetter?: boolean;
  trend?: SeriesKey; // draw a sparkline of this daily series
  detail?: string; // a breakdown line under the number
}
interface Insight {
  tone: "good" | "bad" | "info" | "warning";
  text: string;
}
const INSIGHT_STYLE: Record<Insight["tone"], [string, string]> = {
  good: ["▲", "var(--good)"],
  bad: ["▼", "var(--critical)"],
  warning: ["!", "var(--warning)"],
  info: ["★", "var(--series-1)"],
};

export default function Dashboard() {
  const { lang, t } = useI18n();
  const [allowed, setAllowed] = useState<boolean | null>(null);
  const [filters, setFilters] = useState<DashboardFilters>(NO_FILTERS);
  const [data, setData] = useState<Dashboard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [params, setParams] = useSearchParams(); // ?tab=pipeline, so each tab has a link
  const tab: Tab = params.get("tab") === "pipeline" ? "pipeline" : "audience";

  useEffect(() => {
    api.adminAccess().then((a) => setAllowed(a.admin)).catch(() => setAllowed(false));
  }, []);
  useEffect(() => {
    if (!allowed) return;
    setError(null);
    api.getDashboard(filters).then(setData).catch((e: Error) => setError(e.message));
  }, [allowed, filters]);

  if (allowed === false) return <p className="text-neutral-500">{t("admin.denied")}</p>;
  if (error) return <p className="text-red-600">{error}</p>;
  if (!data) return <p className="text-neutral-500">{t("loading")}</p>;

  // --- formatting ----------------------------------------------------------------------------
  const num = new Intl.NumberFormat(lang);
  const dec = new Intl.NumberFormat(lang, { maximumFractionDigits: 1 });
  const usd = new Intl.NumberFormat(lang, { style: "currency", currency: "USD", maximumFractionDigits: 2 });
  const usdSmall = new Intl.NumberFormat(lang, { style: "currency", currency: "USD", maximumSignificantDigits: 2 });
  const duration = (s: number) => {
    const m = Math.floor(s / 60);
    const sec = Math.round(s - m * 60);
    return m ? `${m}m ${String(sec).padStart(2, "0")}s` : `${dec.format(s)}s`;
  };
  const fmt = (v: number | null, kind: Kind): string =>
    v === null
      ? "–"
      : kind === "rate"
        ? `${dec.format(v * 100)}%`
        : kind === "money"
          ? (v !== 0 && Math.abs(v) < 0.1 ? usdSmall : usd).format(v)
          : kind === "seconds"
            ? duration(v)
          : kind === "number"
            ? dec.format(v)
            : num.format(Math.round(v));
  const day = (d: string) => new Date(`${d}T00:00:00`).toLocaleDateString(lang, { day: "numeric", month: "short" });
  const vs = filters.days === 30 ? t("admin.vsMonth") : t("admin.vsPrevious", { n: filters.days });

  /** Signed change: percentage points for rates, percent for the rest (null if no base). */
  const change = (cur: number | null, prev: number | null, kind: Kind): number | null =>
    cur === null || prev === null || (kind !== "rate" && prev === 0)
      ? null
      : kind === "rate"
        ? (cur - prev) * 100
        : ((cur - prev) / prev) * 100;
  const changeText = (d: number, kind: Kind) => `${d > 0 ? "+" : d < 0 ? "−" : ""}${dec.format(Math.abs(d))}${kind === "rate" ? " pp" : "%"}`;

  const Delta = ({ cur, prev, kind, lowerIsBetter = false, withLabel = true }: {
    cur: number | null;
    prev: number | null;
    kind: Kind;
    lowerIsBetter?: boolean;
    withLabel?: boolean;
  }) => {
    const d = change(cur, prev, kind);
    const title = t("admin.previous", { value: fmt(prev, kind) });
    if (d === null)
      return (
        <span title={title} className="text-xs text-neutral-400">
          — {withLabel && vs}
        </span>
      );
    const dir = Math.abs(d) < 0.05 ? 0 : Math.sign(d);
    const good = dir !== 0 && dir > 0 !== lowerIsBetter;
    const color = dir === 0 ? "text-neutral-500" : good ? "text-green-700 dark:text-green-400" : "text-red-600 dark:text-red-400";
    return (
      <span title={title} className="text-xs whitespace-nowrap">
        <span className={`font-medium ${color}`}>
          {dir > 0 ? "▲" : dir < 0 ? "▼" : "→"} {dec.format(Math.abs(d))}
          {kind === "rate" ? " pp" : "%"}
        </span>
        {withLabel && <span className="text-neutral-500"> {vs}</span>}
      </span>
    );
  };

  const set = (changes: Partial<DashboardFilters>) => setFilters((f) => ({ ...f, ...changes }));
  const { current: cur, previous: prev, series, previous_series: prevSeries, health, options } = data;
  const filtered = filters.language || filters.voice || filters.topic || filters.days !== NO_FILTERS.days;
  const costNote = data.cost_filtered ? undefined : t("admin.costNotFiltered");
  const lineLabels = {
    current: t("admin.currentPeriod", { n: filters.days }),
    previous: t("admin.previousPeriod", { n: filters.days }),
    today: t("admin.today"),
  };

  // --- KPI cards -----------------------------------------------------------------------------
  const sections: { title: string; kpis: Kpi[] }[] = [
    {
      title: t("admin.section.audience"),
      kpis: [
        { key: "users", label: t("admin.users"), kind: "count", trend: "users" },
        { key: "listeners", label: t("admin.engagedUsers"), kind: "count", trend: "listeners" },
        { key: "retention", label: t("admin.retention"), kind: "rate" },
        { key: "stickiness", label: t("admin.stickiness"), kind: "number" },
      ],
    },
    {
      title: t("admin.section.engagement"),
      kpis: [
        { key: "listen_rate", label: t("admin.listenRate"), kind: "rate" },
        { key: "start_rate", label: t("admin.startRate"), kind: "rate" },
        { key: "completion_rate", label: t("admin.completionRate"), kind: "rate" },
        { key: "hours", label: t("admin.hours"), kind: "number", trend: "hours" },
        { key: "minutes_per_listener", label: t("admin.minutesPerListener"), kind: "number" },
      ],
    },
    {
      title: t("admin.section.efficiency"),
      kpis: [
        { key: "cost", label: t("admin.cost"), kind: "money", lowerIsBetter: true, trend: "cost" },
        { key: "cost_per_listener", label: t("admin.costPerListener"), kind: "money", lowerIsBetter: true },
        { key: "cost_per_hour", label: t("admin.costPerHour"), kind: "money", lowerIsBetter: true },
        {
          key: "cost_per_episode",
          label: t("admin.costPerEpisode"),
          kind: "money",
          lowerIsBetter: true,
          detail: t("admin.costSplit", { shared: fmt(cur.shared_cost_per_episode, "money"), extra: fmt(cur.extra_cost_per_episode, "money") }),
        },
        {
          key: "generation_median",
          label: t("admin.generationTime"),
          kind: "seconds",
          lowerIsBetter: true,
          detail: t("admin.generationParts", {
            research: fmt(cur.generation_parts.research, "seconds"),
            clips: fmt(cur.generation_parts.clips, "seconds"),
            assembly: fmt(cur.generation_parts.assembly, "seconds"),
          }),
        },
        { key: "generation_p95", label: t("admin.generationP95"), kind: "seconds", lowerIsBetter: true },
        { key: "episodes", label: t("admin.episodes"), kind: "count", trend: "episodes_created" },
        { key: "ready_rate", label: t("admin.readyRate"), kind: "rate" },
        { key: "stories", label: t("admin.stories"), kind: "count" },
        { key: "clips", label: t("admin.clips"), kind: "count", trend: "clips" },
      ],
    },
  ];
  const kpiCard = (k: Kpi) => (
    <div key={k.key} className={`${card} flex flex-col`}>
      <p className="text-sm text-neutral-500">{k.label}</p>
      <div className="mt-1 flex items-baseline justify-between gap-2">
        <p className="text-3xl font-semibold tracking-tight tabular-nums">{fmt(cur[k.key], k.kind)}</p>
        <Delta cur={cur[k.key]} prev={prev[k.key]} kind={k.kind} lowerIsBetter={k.lowerIsBetter} withLabel={false} />
      </div>
      <div className="mt-auto pt-2">
        {k.detail ? (
          <p className="text-xs text-neutral-500">{k.detail}</p>
        ) : k.trend ? (
          <Sparkline current={series[k.trend]} previous={prevSeries[k.trend]} />
        ) : k.kind === "rate" ? (
          <Bullet value={cur[k.key]} previous={prev[k.key]} label={`${fmt(cur[k.key], k.kind)} · ${t("admin.previous", { value: fmt(prev[k.key], k.kind) })}`} />
        ) : (
          <p className="text-xs text-neutral-400">{t("admin.previous", { value: fmt(prev[k.key], k.kind) })}</p>
        )}
      </div>
    </div>
  );

  // --- insights: what moved most, the best day, the weakest funnel step, retention --------
  const insights: Insight[] = [];
  const tabSections = tab === "pipeline" ? sections.slice(2) : sections.slice(0, 2);
  const all = tabSections.flatMap((s) => s.kpis);
  const moves = all
    .map((k) => ({ k, d: change(cur[k.key], prev[k.key], k.kind) }))
    .filter((m): m is { k: Kpi; d: number } => m.d !== null && Math.abs(m.d) >= 0.05)
    .map((m) => ({ ...m, score: (m.k.kind === "rate" ? m.d * 2 : m.d) * (m.k.lowerIsBetter ? -1 : 1) }))
    .sort((a, b) => b.score - a.score);
  for (const m of moves.filter((m) => m.score > 0).slice(0, 2))
    insights.push({ tone: "good", text: t("admin.insight.up", { metric: m.k.label, change: changeText(m.d, m.k.kind), vs }) });
  for (const m of moves.filter((m) => m.score < 0).slice(-2).reverse())
    insights.push({ tone: "bad", text: t("admin.insight.down", { metric: m.k.label, change: changeText(m.d, m.k.kind), vs }) });
  const audienceTab = tab === "audience";
  const peak = series.listeners.reduce((best, v, i) => (v > series.listeners[best] ? i : best), 0);
  if (audienceTab && series.listeners[peak] > 0)
    insights.push({ tone: "info", text: t("admin.insight.peak", { day: day(series.days[peak]), value: num.format(series.listeners[peak]) }) });
  const steps: [string, number][] = [
    [t("admin.delivered"), cur.funnel.delivered],
    [t("admin.started"), cur.funnel.started],
    [t("admin.finished"), cur.funnel.finished],
  ];
  const drops = steps.slice(1).map(([label, n], i) => ({ from: steps[i][0], to: label, rate: steps[i][1] ? n / steps[i][1] : null }));
  const worst = drops.filter((d) => d.rate !== null).sort((a, b) => (a.rate ?? 1) - (b.rate ?? 1))[0];
  if (audienceTab && worst && (worst.rate ?? 1) < 0.9)
    insights.push({ tone: "warning", text: t("admin.insight.funnel", { from: worst.from, to: worst.to, rate: fmt(worst.rate, "rate") }) });
  if (audienceTab && cur.retention !== null && cur.retention < 0.4)
    insights.push({ tone: "warning", text: t("admin.insight.retentionLow", { rate: fmt(cur.retention, "rate") }) });
  const growing = data.topics
    .filter((tp) => tp.previous !== null && tp.followers > (tp.previous ?? 0))
    .sort((a, b) => b.followers - (b.previous ?? 0) - (a.followers - (a.previous ?? 0)))[0];
  if (audienceTab && growing)
    insights.push({ tone: "good", text: t("admin.insight.topic", { topic: growing.label, change: `+${num.format(growing.followers - (growing.previous ?? 0))}` }) });
  if (!audienceTab && health.status !== "good")
    insights.push({
      tone: health.status === "critical" ? "bad" : "warning",
      text: t("admin.insight.health", { status: t(`admin.health.${health.status}`), overdue: num.format(health.overdue), failed: num.format(health.failed_24h + health.dead_24h) }),
    });

  // --- retention: who came back, who is new, who churned --------------------------------------
  const returning = Math.round((cur.retention ?? 0) * prev.listeners);
  const flow = [
    { key: "returning", label: t("admin.returning"), n: returning, color: "var(--series-1)" },
    { key: "new", label: t("admin.newListeners"), n: Math.max(0, cur.listeners - returning), color: "var(--good)" },
    { key: "churned", label: t("admin.churned"), n: Math.max(0, prev.listeners - returning), color: "var(--previous)" },
  ];
  const flowTotal = flow.reduce((s, f) => s + f.n, 0);
  const weeks = data.retention_weeks;

  const lineTable = (key: SeriesKey, kind: Kind) => (
    <DataTable
      columns={[t("admin.day"), lineLabels.current, lineLabels.previous]}
      rows={[...series.days.keys()].reverse().map((i) => [
        day(series.days[i]),
        fmt(series[key][i], kind),
        `${fmt(prevSeries[key][i], kind)} (${day(prevSeries.days[i])})`,
      ])}
    />
  );
  const line = (key: SeriesKey, kind: Kind) => (
    <LineChart
      days={series.days}
      previousDays={prevSeries.days}
      current={series[key]}
      previous={prevSeries[key]}
      format={(v) => fmt(v, kind)}
      formatDay={day}
      labels={lineLabels}
    />
  );
  const ranked = (rows: { label: string; value: number; previous?: number }[], kind: Kind, lowerIsBetter = false) => ({
    chart: (
      <RankedBars
        rows={rows.map((r) => ({
          ...r,
          extra:
            r.previous === undefined ? undefined : (
              <Delta cur={r.value} prev={r.previous} kind={kind} lowerIsBetter={lowerIsBetter} withLabel={false} />
            ),
        }))}
        format={(v) => fmt(v, kind)}
        emptyText={t("admin.empty")}
      />
    ),
    table: (
      <DataTable
        columns={rows.some((r) => r.previous !== undefined) ? ["", t("admin.value"), t("admin.previousColumn")] : ["", t("admin.value")]}
        rows={rows.map((r) => [r.label, fmt(r.value, kind), ...(r.previous === undefined ? [] : [fmt(r.previous, kind)])])}
      />
    ),
  });
  const stages = [...new Set([...Object.keys(cur.cost_by_stage), ...Object.keys(prev.cost_by_stage)])]
    .map((stage) => ({ label: stage, value: cur.cost_by_stage[stage] ?? 0, previous: prev.cost_by_stage[stage] ?? 0 }))
    .sort((a, b) => b.value - a.value);

  return (
    <section className="space-y-10">
      <div>
        <h1 className="font-serif text-4xl font-medium tracking-tight">{t("admin.title")}</h1>
        <p className="mt-1 text-neutral-500">{t("admin.subtitle")}</p>
      </div>

      {/* 1. Control panel: every filter in one row */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex rounded-full bg-neutral-100 p-1 dark:bg-neutral-800" role="group" aria-label={t("admin.period")}>
          {PERIODS.map((d) => (
            <button
              key={d}
              onClick={() => set({ days: d })}
              aria-pressed={filters.days === d}
              className={`rounded-full px-3 py-1 text-xs font-medium transition ${
                filters.days === d
                  ? "bg-white text-neutral-900 shadow-sm dark:bg-neutral-950 dark:text-white"
                  : "text-neutral-500 hover:text-neutral-900 dark:hover:text-white"
              }`}
            >
              {t("admin.days", { n: d })}
            </button>
          ))}
        </div>
        <select className={select} value={filters.language} onChange={(e) => set({ language: e.target.value })}>
          <option value="">{t("admin.allLanguages")}</option>
          {options.languages.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        <select className={select} value={filters.voice} onChange={(e) => set({ voice: e.target.value })}>
          <option value="">{t("admin.allVoices")}</option>
          {options.voices.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        <select className={select} value={filters.topic} onChange={(e) => set({ topic: e.target.value })}>
          <option value="">{t("admin.allTopics")}</option>
          {options.topics.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        {filtered && (
          <button onClick={() => setFilters(NO_FILTERS)} className="text-sm font-medium text-brand-600 hover:text-brand-800 dark:text-brand-400">
            {t("admin.reset")}
          </button>
        )}
        <span className="ml-auto text-xs text-neutral-500">
          {day(data.range.from)} – {day(data.range.to)} · {vs}: {day(data.range.previous_from)} – {day(data.range.previous_to)}
        </span>
      </div>

      {/* Tabs: audience & engagement, pipeline health */}
      <div className="flex gap-1 border-b border-neutral-200 dark:border-neutral-800" role="tablist">
        {(["audience", "pipeline"] as const).map((id) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            onClick={() => setParams(id === "audience" ? {} : { tab: id }, { replace: true })}
            className={`-mb-px border-b-2 px-4 py-2.5 text-sm font-medium transition ${
              tab === id
                ? "border-brand-600 text-neutral-900 dark:border-brand-400 dark:text-white"
                : "border-transparent text-neutral-500 hover:text-neutral-900 dark:hover:text-white"
            }`}
          >
            {t(`admin.tab.${id}`)}
          </button>
        ))}
      </div>

      {tab === "audience" ? (
        <>
          {/* 2. Insights */}
          <div className={`${card} border-brand-200 bg-gradient-to-br from-brand-50 to-white dark:border-brand-900 dark:from-brand-950/40 dark:to-neutral-900`}>
            <h2 className="font-semibold">{t("admin.insights")}</h2>
            {insights.length ? (
              <ul className="mt-3 grid gap-3 md:grid-cols-2">
                {insights.map((ins, i) => (
                  <li key={i} className="flex items-start gap-3 text-sm">
                    <span
                      className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-bold text-white"
                      style={{ background: INSIGHT_STYLE[ins.tone][1] }}
                      aria-hidden
                    >
                      {INSIGHT_STYLE[ins.tone][0]}
                    </span>
                    {ins.text}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-2 text-sm text-neutral-500">{t("admin.insight.none")}</p>
            )}
          </div>

          {/* 3. Engaged users and new users: current period vs the previous one */}
          <div className="grid gap-4 lg:grid-cols-2">
            <Panel title={t("admin.engagedUsersByDay")} value={fmt(cur.listeners, "count")} delta={<Delta cur={cur.listeners} prev={prev.listeners} kind="count" />}
              chart={line("listeners", "count")} table={lineTable("listeners", "count")} />
            <Panel title={t("admin.newUsersByDay")} value={fmt(cur.new_users, "count")} delta={<Delta cur={cur.new_users} prev={prev.new_users} kind="count" />}
              chart={line("new_users", "count")} table={lineTable("new_users", "count")} />
          </div>

          {sections.slice(0, 2).map((s) => (
            <div key={s.title}>
              <h2 className="mb-3 text-sm font-semibold tracking-wide text-neutral-500 uppercase">{s.title}</h2>
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">{s.kpis.map(kpiCard)}</div>
            </div>
          ))}

          {/* 5. Retention and engagement */}
          <div>
            <h2 className="mb-3 text-sm font-semibold tracking-wide text-neutral-500 uppercase">{t("admin.section.retention")}</h2>
            <div className="grid gap-4 lg:grid-cols-3">
              <div className="lg:col-span-2">
                <Panel
                  title={t("admin.retentionWeekly")}
                  note={t("admin.retentionWeeklyHint")}
                  value={fmt(weeks[weeks.length - 1]?.rate ?? null, "rate")}
                  delta={<Delta cur={weeks[weeks.length - 1]?.rate ?? null} prev={weeks[weeks.length - 2]?.rate ?? null} kind="rate" withLabel={false} />}
                  chart={
                    !weeks.some((w) => w.rate !== null) ? (
                      <p className="flex h-40 items-center justify-center text-sm text-neutral-400">{t("admin.empty")}</p>
                    ) : (
                    <LineChart
                      days={weeks.map((w) => w.week)}
                      current={weeks.map((w) => w.rate ?? 0)}
                      format={(v) => fmt(v, "rate")}
                      formatDay={(d) => t("admin.week", { day: day(d) })}
                      labels={{ current: t("admin.retention"), today: t("admin.thisWeek") }}
                      height={180}
                    />
                    )
                  }
                  table={
                    <DataTable
                      columns={["", t("admin.retention"), t("admin.returning"), t("admin.newListeners")]}
                      rows={[...weeks].reverse().map((w) => [t("admin.week", { day: day(w.week) }), fmt(w.rate, "rate"), num.format(w.returning), num.format(w.new)])}
                    />
                  }
                />
              </div>
              <div className={card}>
                <h2 className="font-semibold">{t("admin.listenerFlow")}</h2>
                <p className="mt-0.5 text-xs text-neutral-500">{vs}</p>
                {flowTotal ? (
                  <>
                    <div className="mt-4 flex h-4 gap-[2px] overflow-hidden rounded-full" role="img" aria-label={flow.map((f) => `${f.label} ${f.n}`).join(", ")}>
                      {flow.filter((f) => f.n).map((f) => (
                        <div key={f.key} style={{ width: `${(f.n / flowTotal) * 100}%`, background: f.color }} />
                      ))}
                    </div>
                    <ul className="mt-4 space-y-2 text-sm">
                      {flow.map((f) => (
                        <li key={f.key} className="flex items-center gap-2">
                          <span className="h-2.5 w-2.5 rounded-sm" style={{ background: f.color }} />
                          <span className="text-neutral-600 dark:text-neutral-300">{f.label}</span>
                          <span className="ml-auto font-semibold tabular-nums">{num.format(f.n)}</span>
                        </li>
                      ))}
                    </ul>
                  </>
                ) : (
                  <p className="mt-4 text-sm text-neutral-400">{t("admin.empty")}</p>
                )}
              </div>
              <Panel title={t("admin.funnel")} value={fmt(cur.completion_rate, "rate")} valueLabel={t("admin.completionRate")}
                delta={<Delta cur={cur.completion_rate} prev={prev.completion_rate} kind="rate" />}
                {...ranked([
                  { label: t("admin.delivered"), value: cur.funnel.delivered, previous: prev.funnel.delivered },
                  { label: t("admin.started"), value: cur.funnel.started, previous: prev.funnel.started },
                  { label: t("admin.finished"), value: cur.funnel.finished, previous: prev.funnel.finished },
                ], "count")} />
              <div className="lg:col-span-2">
                <Panel title={t("admin.hoursByDay")} value={fmt(cur.hours, "number")} delta={<Delta cur={cur.hours} prev={prev.hours} kind="number" />}
                  chart={line("hours", "number")} table={lineTable("hours", "number")} />
              </div>
            </div>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <Panel title={t("admin.topTopics")} note={data.topics_filtered ? undefined : t("admin.topicsAll")}
                {...ranked(data.topics.map((tp) => ({ label: tp.label, value: tp.followers, previous: tp.previous ?? undefined })), "count")} />
          </div>
        </>
      ) : (
        <>
          {/* 2. Insights */}
          <div className={`${card} border-brand-200 bg-gradient-to-br from-brand-50 to-white dark:border-brand-900 dark:from-brand-950/40 dark:to-neutral-900`}>
            <h2 className="font-semibold">{t("admin.insights")}</h2>
            {insights.length ? (
              <ul className="mt-3 grid gap-3 md:grid-cols-2">
                {insights.map((ins, i) => (
                  <li key={i} className="flex items-start gap-3 text-sm">
                    <span
                      className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-bold text-white"
                      style={{ background: INSIGHT_STYLE[ins.tone][1] }}
                      aria-hidden
                    >
                      {INSIGHT_STYLE[ins.tone][0]}
                    </span>
                    {ins.text}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-2 text-sm text-neutral-500">{t("admin.insight.none")}</p>
            )}
          </div>

          <div className={card}>
              <h2 className="font-semibold">{t("admin.health")}</h2>
              <div className="mt-4 flex flex-wrap items-center gap-x-8 gap-y-4">
                <span className="flex items-center gap-2 font-medium">
                  <span
                    className="flex h-7 w-7 items-center justify-center rounded-full text-sm font-bold text-white"
                    style={{ background: `var(--${health.status})` }}
                    aria-hidden
                  >
                    {STATUS_ICON[health.status]}
                  </span>
                  {t(`admin.health.${health.status}`)}
                </span>
                {[
                  [t("admin.queue"), num.format(health.jobs.queued)],
                  [t("admin.running"), num.format(health.jobs.running)],
                  [t("admin.overdue"), num.format(health.overdue)],
                  [t("admin.dead24"), num.format(health.dead_24h)],
                  [t("admin.failed24"), num.format(health.failed_24h)],
                  [t("admin.lastJob"), health.last_job_at ? new Date(health.last_job_at).toLocaleString(lang, { dateStyle: "short", timeStyle: "short" }) : t("admin.never")],
                  [t("admin.budget"), `${usd.format(health.spent_today)} ${t("admin.ofBudget", { budget: usd.format(health.daily_budget) })}`],
                ].map(([label, value]) => (
                  <div key={label}>
                    <p className="text-xs text-neutral-500">{label}</p>
                    <p className="font-medium tabular-nums">{value}</p>
                  </div>
                ))}
              </div>
            </div>

          {sections.slice(2).map((s) => (
            <div key={s.title}>
              <h2 className="mb-3 text-sm font-semibold tracking-wide text-neutral-500 uppercase">{s.title}</h2>
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">{s.kpis.map(kpiCard)}</div>
            </div>
          ))}

          <div className="grid gap-4 lg:grid-cols-2">
              <Panel title={t("admin.episodesByDay")} value={fmt(cur.episodes, "count")} delta={<Delta cur={cur.episodes} prev={prev.episodes} kind="count" />}
                chart={
                  <DailyBars
                    days={series.days}
                    series={[
                      { key: "ready", label: t("admin.ready"), color: "var(--good)", values: Object.fromEntries(series.days.map((d, i) => [d, series.episodes_ready[i]])) },
                      { key: "failed", label: t("admin.failed"), color: "var(--critical)", values: Object.fromEntries(series.days.map((d, i) => [d, series.episodes_failed[i]])) },
                    ]}
                    format={(v) => num.format(v)}
                    formatDay={day}
                    emptyText={t("admin.empty")}
                  />
                }
                table={
                  <DataTable
                    columns={[t("admin.day"), t("admin.ready"), t("admin.failed")]}
                    rows={[...series.days.keys()].reverse().map((i) => [day(series.days[i]), num.format(series.episodes_ready[i]), num.format(series.episodes_failed[i])])}
                  />
                } />
              <Panel title={t("admin.costByDay")} note={costNote} value={fmt(cur.cost, "money")} delta={<Delta cur={cur.cost} prev={prev.cost} kind="money" lowerIsBetter />}
                chart={line("cost", "money")} table={lineTable("cost", "money")} />
              <Panel title={t("admin.costByStage")} note={costNote} value={fmt(cur.cost, "money")} delta={<Delta cur={cur.cost} prev={prev.cost} kind="money" lowerIsBetter />}
                {...ranked(stages, "money", true)} />
          </div>
        </>
      )}
    </section>
  );
}

/** A chart card: title, headline value with its change, and a Chart / Table toggle. */
function Panel({ title, note, value, valueLabel, delta, chart, table }: {
  title: string;
  note?: string;
  value?: string;
  valueLabel?: string;
  delta?: ReactNode;
  chart: ReactNode;
  table: ReactNode;
}) {
  const { t } = useI18n();
  const [view, setView] = useState<"chart" | "table">("chart");
  return (
    <div className={`${card} h-full`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="font-semibold">{title}</h2>
          {value !== undefined && (
            <p className="mt-1 flex flex-wrap items-baseline gap-x-2">
              <span className="text-xl font-semibold tabular-nums">{value}</span>
              {valueLabel && <span className="text-xs text-neutral-500">{valueLabel}</span>}
              {delta}
            </p>
          )}
          {note && <p className="mt-0.5 text-xs text-neutral-500">{note}</p>}
        </div>
        <div className="flex shrink-0 rounded-full bg-neutral-100 p-0.5 text-xs dark:bg-neutral-800" role="group">
          {(["chart", "table"] as const).map((v) => (
            <button
              key={v}
              onClick={() => setView(v)}
              aria-pressed={view === v}
              className={`rounded-full px-2.5 py-1 font-medium transition ${
                view === v ? "bg-white shadow-sm dark:bg-neutral-950" : "text-neutral-500 hover:text-neutral-900 dark:hover:text-white"
              }`}
            >
              {t(`admin.${v}`)}
            </button>
          ))}
        </div>
      </div>
      <div className="mt-4">{view === "chart" ? chart : table}</div>
    </div>
  );
}
