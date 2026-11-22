// Main page: what's new at the top, then one row per topic, then episodes to listen to again.
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api, type Episode } from "../api";
import EpisodeRow from "../components/EpisodeRow";
import { useI18n } from "../i18n";

export default function Home() {
  const { t, topicName } = useI18n();
  const [episodes, setEpisodes] = useState<Episode[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.listEpisodes("").then(setEpisodes).catch((e: Error) => setError(e.message));
  }, []);

  // Keep the page in step when the player marks an episode as listened.
  const replace = (updated: Episode) =>
    setEpisodes((list) => list?.map((e) => (e.id === updated.id ? updated : e)) ?? null);

  if (error) return <p className="text-red-600">{t("episodes.error")} {error}</p>;
  if (!episodes) return <p className="text-neutral-500">{t("loading")}</p>;
  if (episodes.length === 0)
    return (
      <p className="rounded-2xl border border-dashed border-neutral-300 p-10 text-center text-neutral-500 dark:border-neutral-700">
        {t("home.none")}{" "}
        <Link to="/settings" className="font-medium text-brand-600 dark:text-brand-400">
          {t("menu.preferences")} ›
        </Link>
      </p>
    );

  // The API returns newest first, so every group below stays newest first too.
  const fresh = episodes.filter((e) => !e.listened);
  const heard = episodes.filter((e) => e.listened);
  const topics = new Map<string, { label: string; episodes: Episode[] }>();
  for (const ep of episodes) {
    const group = topics.get(ep.topic) ?? { label: topicName(ep), episodes: [] };
    group.episodes.push(ep);
    topics.set(ep.topic, group);
  }

  return (
    <div className="space-y-12">
      {fresh.length > 0 ? (
        <EpisodeRow
          title={t("episodes.title")}
          subtitle={t("episodes.subtitle")}
          episodes={fresh}
          large
          onListened={replace}
        />
      ) : (
        <section>
          <h2 className="text-2xl font-semibold tracking-tight">{t("episodes.title")}</h2>
          <p className="mt-3 rounded-2xl border border-dashed border-neutral-300 p-6 text-center text-neutral-500 dark:border-neutral-700">
            {t("episodes.empty")}
          </p>
        </section>
      )}

      {[...topics].map(([slug, group]) => (
        <EpisodeRow
          key={slug}
          title={group.label}
          episodes={group.episodes}
          seeAllTo={`/topics/${slug}`}
          showTopic={false}
          onListened={replace}
        />
      ))}

      {heard.length > 0 && <EpisodeRow title={t("home.again")} episodes={heard} onListened={replace} />}
    </div>
  );
}
