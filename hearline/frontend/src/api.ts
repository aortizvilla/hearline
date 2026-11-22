import { STATIC_DEMO, staticRequest } from "./staticDemo";

// A suggested subcategory: canonical English label (what is saved) + its name per language
export interface Suggestion {
  label: string;
  labels: Record<string, string>;
}

// The listener's private podcast feed (backend app/api/feed.py)
export interface PodcastLinks {
  feed_url: string;
  apple_podcasts_url: string;
}

export interface Interest {
  slug: string;
  label: string;
  image: string;
  labels?: Record<string, string>; // name per interface language (dim_topic.labels)
  suggestions?: Suggestion[]; // this topic's suggested subcategories (config/subtopics.yaml)
}

export type Frequency = "daily" | "weekdays" | "weekly";

export interface TopicPref {
  topic: string;
  frequency: Frequency;
  target_minutes: number;
  subtopics?: string[]; // focus subcategories, e.g. "Cannes Festival" under Film
  subtopic_labels?: Record<string, Record<string, string>>; // returned: name per language
}

export interface Profile {
  name: string;
  email: string;
  tone: string;
  language: string;
  voice: string;
  topics: TopicPref[];
  plan?: string; // returned only: the current plan's key (change it with api.changePlan)
}

// A subscription plan (backend config/plans.yaml). "all" = no restriction; null = no limit.
export interface Plan {
  key: string;
  label: string;
  price_usd_month: number;
  max_topics: number | null;
  max_topic_minutes: number;
  max_episode_minutes: number;
  subtopics: boolean;
  voices: string[] | "all";
  tones: string[] | "all";
}

export interface Plans {
  plans: Plan[];
  self_serve: boolean; // users may switch plan themselves (no billing yet)
}

export const allows = (list: string[] | "all", value: string): boolean =>
  list === "all" || list.includes(value);

export interface Option {
  value: string;
  label: string;
  description?: string;
}

// A voice from the dim_voice table (backend config/voices.yaml)
export interface VoiceOption extends Option {
  gender: string;
  accent: string; // of its English voice
  accents: Record<string, string>; // language -> accent of that language's native speaker
  previews: Record<string, string>; // language -> sample link (`app.cli voice-previews`)
}

export interface Options {
  tones: Option[];
  languages: Option[];
  voices: VoiceOption[];
}

export interface Episode {
  id: number;
  topic: string;
  topic_label: string;
  topic_labels?: Record<string, string>; // topic name per interface language
  title: string;
  description: string;
  image_url: string | null;
  audio_url: string | null;
  duration_seconds: number | null;
  listened: boolean;
  progress_seconds: number; // furthest point heard, to resume there
  status: string;
  published_at: string;
}

export type HealthStatus = "good" | "warning" | "critical";

/** A window day by day: every list lines up with `days` (day i of the current window with
 * day i of the previous one). */
export interface DailySeries {
  days: string[];
  users: number[]; // running total of sign-ups
  new_users: number[];
  listeners: number[];
  hours: number[];
  cost: number[];
  cost_per_episode: (number | null)[]; // null: no episode that day
  generation_median: (number | null)[];
  episodes_created: number[];
  episodes_ready: number[];
  episodes_failed: number[];
  delivered: number[];
  delivered_started: number[];
  delivered_finished: number[];
  delivered_seconds_heard: number[];
  delivered_seconds_started: number[];
  clips: number[];
}

/** One window's headline numbers (backend app/tracking/dashboard.py period_kpis). */
export interface PeriodKpis {
  users: number;
  new_users: number;
  listeners: number;
  retention: number | null;
  stickiness: number | null;
  listen_rate: number | null;
  start_rate: number | null;
  completion_rate: number | null;
  hours: number;
  minutes_per_listener: number | null;
  episodes: number;
  ready_rate: number | null;
  failed: number;
  funnel: { delivered: number; started: number; finished: number };
  cost: number;
  cost_by_stage: Record<string, number>;
  cost_per_listener: number | null;
  cost_per_hour: number | null;
  cost_per_episode: number | null; // all cost / episodes made ready
  shared_cost_per_episode: number | null; // research + scripts, shared by every listener
  extra_cost_per_episode: number | null; // what one more episode adds (phrases, review)
  generation_median: number | null; // seconds to generate an episode end to end
  generation_p95: number | null;
  generation_parts: { research: number | null; clips: number | null; assembly: number | null };
  stories: number;
  clips: number;
}

/** GET /api/admin/dashboard. Days are "YYYY-MM-DD"; `previous` is the window just before. */
export interface Dashboard {
  range: { from: string; to: string; previous_from: string; previous_to: string; days: string[] };
  current: PeriodKpis;
  previous: PeriodKpis;
  series: DailySeries;
  previous_series: DailySeries;
  retention_weeks: { week: string; listeners: number; returning: number; new: number; rate: number | null }[];
  topics: { key: string; label: string; followers: number; previous: number | null }[];
  cost_filtered: boolean;
  topics_filtered: boolean;
  health: {
    status: HealthStatus;
    jobs: Record<"queued" | "running" | "done" | "dead", number>;
    overdue: number;
    dead_24h: number;
    failed_24h: number;
    last_job_at: string | null;
    spent_today: number;
    daily_budget: number;
  };
  options: { languages: Option[]; voices: Option[]; topics: Option[] };
}

export interface DashboardFilters {
  days: number;
  language: string;
  voice: string;
  topic: string;
}

// Set by auth.tsx when Clerk login is on: every request then carries the session token.
type TokenGetter = () => Promise<string | null>;
let tokenGetter: TokenGetter | null = null;

export function setTokenGetter(getter: TokenGetter | null): void {
  tokenGetter = getter;
}

// Local development without Clerk: who is signed in by email (POST /api/dev/login). Stored
// per browser: a user id, "default" (continue without signing in), or nothing (show sign-in).
const DEV_USER_KEY = "hearline.devUser";
let devUser: string | null = (() => {
  try {
    return localStorage.getItem(DEV_USER_KEY);
  } catch {
    return null;
  }
})();

export function getDevUser(): string | null {
  return devUser;
}

export function setDevUser(value: string | null): void {
  devUser = value;
  try {
    if (value === null) localStorage.removeItem(DEV_USER_KEY);
    else localStorage.setItem(DEV_USER_KEY, value);
  } catch {
    // private mode / blocked storage: the choice lasts for this page only
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  if (STATIC_DEMO) return staticRequest<T>(path, init); // static demo site: no API, demo files
  const token = tokenGetter ? await tokenGetter() : null;
  const asUser: Record<string, string> =
    !token && devUser && devUser !== "default" ? { "X-User-Id": devUser } : {};
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...asUser,
      ...init?.headers,
    },
  });
  if (!response.ok) {
    // FastAPI puts a readable reason in `detail` (e.g. a subcategory that doesn't fit)
    const body = await response.json().catch(() => null);
    const detail = typeof body?.detail === "string" ? body.detail : "";
    throw new Error(detail || `${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

export const api = {
  getInterests: (): Promise<Interest[]> => request("/interests"),
  getOptions: (): Promise<Options> => request("/options"),
  getProfile: (): Promise<Profile> => request("/profile"),
  getPlans: (): Promise<Plans> => request("/plans"),
  changePlan: (plan: string): Promise<Profile> =>
    request("/plan", { method: "PUT", body: JSON.stringify({ plan }) }),
  devLogin: (email: string): Promise<{ user_id: number; name: string; email: string }> =>
    request("/dev/login", { method: "POST", body: JSON.stringify({ email }) }),
  getPodcast: (): Promise<PodcastLinks> => request("/podcast"),
  resetPodcast: (): Promise<PodcastLinks> => request("/podcast/reset", { method: "POST" }),
  updateProfile: (profile: Profile): Promise<Profile> =>
    request("/profile", { method: "PUT", body: JSON.stringify(profile) }),
  listEpisodes: (query: string): Promise<Episode[]> => request(`/episodes?${query}`),
  saveProgress: (id: number, seconds: number): Promise<Episode> =>
    request(`/episodes/${id}/progress`, { method: "POST", body: JSON.stringify({ seconds: Math.floor(seconds) }) }),
  adminAccess: (): Promise<{ admin: boolean }> => request("/admin/access"),
  getDashboard: (f: DashboardFilters): Promise<Dashboard> =>
    request(`/admin/dashboard?${new URLSearchParams({ ...f, days: String(f.days) })}`),
  markListened: (id: number): Promise<Episode> =>
    request(`/episodes/${id}/listened`, { method: "POST" }),
  // erases the account and everything that is theirs (episodes, audio, settings, history)
  deleteAccount: (): Promise<{ deleted: boolean }> => request("/account", { method: "DELETE" }),
};
