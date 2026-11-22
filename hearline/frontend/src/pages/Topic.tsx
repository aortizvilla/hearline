import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api, type Episode, type Interest } from "../api";
import EpisodeCard from "../components/EpisodeCard";
import { useI18n } from "../i18n";

export default function Topic() {
  const { slug = "" } = useParams();
  const { t, name } = useI18n();
  const [topic, setTopic] = useState<Interest | null>(null);
  const [episodes, setEpisodes] = useState<Episode[] | null>(null);

  useEffect(() => {
    setEpisodes(null);
    api.getInterests().then((all) => setTopic(all.find((i) => i.slug === slug) ?? null));
    api.listEpisodes(`topic=${encodeURIComponent(slug)}`).then(setEpisodes);
  }, [slug]);

  const markListened = (id: number) => {
    api
      .markListened(id)
      .then(() =>
        setEpisodes((eps) => eps?.map((e) => (e.id === id ? { ...e, listened: true } : e)) ?? null),
      );
  };

  return (
    <section>
      <Link to="/" className="text-sm text-neutral-500 hover:text-neutral-900 dark:hover:text-white">
        {t("topic.back")}
      </Link>
      <div className="mt-4 mb-8 flex items-center gap-4">
        {topic && (
          <img
            src={topic.image}
            alt=""
            referrerPolicy="no-referrer"
            className="h-16 w-16 rounded-2xl object-cover"
          />
        )}
        <div>
          <h1 className="font-serif text-4xl font-medium tracking-tight">{topic ? name(topic.label, topic.labels) : slug}</h1>
          <p className="text-neutral-500">{t("topic.subtitle")}</p>
        </div>
      </div>
      {episodes?.length === 0 && (
        <p className="rounded-2xl border border-dashed border-neutral-300 p-10 text-center text-neutral-500 dark:border-neutral-700">
          {t("topic.empty")}
        </p>
      )}
      <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
        {episodes?.map((ep) => (
          <EpisodeCard key={ep.id} episode={ep} onListened={() => markListened(ep.id)} />
        ))}
      </div>
    </section>
  );
}
