// Static demo: the app without a server, for a static host. Built with VITE_STATIC_DEMO=1, every
// API call is answered from the snapshot made by the backend's `python -m app.cli export-demo`
// (frontend/demo-data/, served at <base>demo/; normal builds leave it out): the demo listeners'
// profiles, episodes and audio, the catalogs and a dashboard snapshot. What a visitor changes
// (settings, plan, listening progress) is kept in their own browser only. No keys, no database,
// nothing to run.

export const STATIC_DEMO = import.meta.env.VITE_STATIC_DEMO === "1";
const BASE = import.meta.env.BASE_URL; // "/" (Cloudflare Pages, locally) or VITE_BASE
const DEMO = `${BASE}demo/`;

/** A public file (public/media/...) under the site's base path. */
export const asset = (path: string): string => `${BASE}${path.replace(/^\//, "")}`;

interface Listener {
  user_id: number;
  name: string;
  email: string;
}
type Json = Record<string, unknown>;

const cache = new Map<string, Promise<unknown>>();
function file<T>(path: string): Promise<T> {
  if (!cache.has(path))
    cache.set(
      path,
      fetch(`${DEMO}${path}`)
        .then((r) => {
          if (!r.ok) throw new Error(`Demo file missing: ${path}`);
          return r.text();
        })
        // audio links in the snapshot are /audio/...; on a static host they live in demo/audio/
        .then((text) => JSON.parse(text.replaceAll('"/audio/', `"${DEMO}audio/`))),
    );
  return cache.get(path) as Promise<T>;
}

// Per-visitor changes, by listener: profile edits, plan, progress and "listened" per episode
const read = (key: string): Json => {
  try {
    return JSON.parse(localStorage.getItem(key) ?? "{}") as Json;
  } catch {
    return {};
  }
};
const write = (key: string, value: Json) => {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // storage blocked: the change lasts until the page reloads
  }
};
const profileKey = (uid: number) => `hearline.demo.profile.${uid}`;
const episodesKey = (uid: number) => `hearline.demo.episodes.${uid}`;

async function currentListener(): Promise<Listener> {
  const { listeners } = await file<{ listeners: Listener[] }>("index.json");
  let id: string | null = null;
  try {
    id = localStorage.getItem("hearline.devUser");
  } catch {
    // no storage: the first listener
  }
  return listeners.find((l) => String(l.user_id) === id) ?? listeners[0];
}

async function profile(uid: number): Promise<Json> {
  return { ...(await file<Json>(`u/${uid}/profile.json`)), ...read(profileKey(uid)) };
}

async function episodes(uid: number): Promise<Json[]> {
  const local = read(episodesKey(uid)) as Record<string, Json>;
  return (await file<Json[]>(`u/${uid}/episodes.json`)).map((e) => ({ ...e, ...local[String(e.id)] }));
}

async function updateEpisode(uid: number, id: number, change: (e: Json) => Json): Promise<Json> {
  const episode = (await episodes(uid)).find((e) => e.id === id);
  if (!episode) throw new Error("Episode not found.");
  const local = read(episodesKey(uid));
  const updated = change(episode);
  write(episodesKey(uid), { ...local, [id]: { ...(local[id] as Json), ...updated } });
  return { ...episode, ...updated };
}

/** Answers one API call from the snapshot (same paths and shapes as the real API). */
export async function staticRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const method = init?.method ?? "GET";
  const body = init?.body ? (JSON.parse(String(init.body)) as Json) : {};
  const [route, query = ""] = path.split("?");
  const params = new URLSearchParams(query);
  const answer = async (): Promise<unknown> => {
    if (method === "GET" && ["/options", "/interests", "/plans"].includes(route)) return file(`api${route}.json`);
    if (route === "/dev/login") {
      const { listeners } = await file<{ listeners: Listener[] }>("index.json");
      const found = listeners.find((l) => l.email.toLowerCase() === String(body.email).trim().toLowerCase());
      if (!found) throw new Error(`This demo has these listeners: ${listeners.map((l) => l.email).join(", ")}`);
      return found;
    }
    if (route === "/admin/access") return { admin: true };
    if (route === "/admin/dashboard") return file("api/admin/dashboard.json");
    const me = await currentListener();
    const uid = me.user_id;
    if (route === "/profile" && method === "GET") return profile(uid);
    if (route === "/profile" && method === "PUT") {
      write(profileKey(uid), body);
      return profile(uid);
    }
    if (route === "/plan") {
      write(profileKey(uid), { ...read(profileKey(uid)), plan: body.plan });
      return profile(uid);
    }
    if (route === "/episodes") {
      const topic = params.get("topic");
      const listened = params.get("listened");
      return (await episodes(uid)).filter(
        (e) => (!topic || e.topic === topic) && (listened === null || String(e.listened) === listened),
      );
    }
    const progress = route.match(/^\/episodes\/(\d+)\/progress$/);
    if (progress)
      return updateEpisode(uid, Number(progress[1]), (e) => ({
        progress_seconds: Math.max(Number(e.progress_seconds ?? 0), Number(body.seconds ?? 0)),
      }));
    const heard = route.match(/^\/episodes\/(\d+)\/listened$/);
    if (heard) return updateEpisode(uid, Number(heard[1]), () => ({ listened: true }));
    if (route.startsWith("/podcast"))
      throw new Error("The private podcast feed needs the full app; this is a static demo.");
    if (route === "/account" && method === "DELETE") {
      write(profileKey(uid), {});
      write(episodesKey(uid), {});
      return { deleted: true };
    }
    throw new Error("Not available in the static demo.");
  };
  return (await answer()) as T;
}
