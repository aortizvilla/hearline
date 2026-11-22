import { Link } from "react-router-dom";

import type { Episode } from "../api";
import { useI18n } from "../i18n";

const formatDate = (iso: string, lang: string): string =>
  new Date(iso).toLocaleString(lang, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });

interface Props {
  episode: Episode;
  onListened?: () => void;
}

export default function EpisodeCard({ episode, onListened }: Props) {
  const { lang, t, topicName } = useI18n();
  const length =
    episode.duration_seconds === null
      ? null
      : t("card.minutes", { n: Math.max(1, Math.round(episode.duration_seconds / 60)) });
  return (
    <article className="flex flex-col overflow-hidden rounded-2xl border border-neutral-200 bg-white shadow-sm dark:border-neutral-800 dark:bg-neutral-900">
      <div className="aspect-video bg-gradient-to-br from-brand-100 to-neutral-100 dark:from-brand-950 dark:to-neutral-800">
        {episode.image_url && (
          <img
            src={episode.image_url}
            alt=""
            loading="lazy"
            referrerPolicy="no-referrer"
            className="h-full w-full object-cover"
          />
        )}
      </div>
      <div className="flex flex-1 flex-col gap-2 p-5">
        <div className="flex items-center gap-2 text-xs">
          <Link
            to={`/topics/${episode.topic}`}
            className="rounded-full bg-brand-50 px-2.5 py-1 font-medium text-brand-700 hover:bg-brand-100 dark:bg-brand-500/15 dark:text-brand-300 dark:hover:bg-brand-500/25"
          >
            {topicName(episode)}
          </Link>
          {length && <span className="text-neutral-500">{length}</span>}
          <time dateTime={episode.published_at} className="text-neutral-400">
            {formatDate(episode.published_at, lang)}
          </time>
          {episode.listened && <span className="ml-auto text-neutral-400">{t("card.listened")}</span>}
        </div>
        <h2 className="line-clamp-2 font-semibold leading-snug">{episode.title}</h2>
        <p className="line-clamp-3 text-sm text-neutral-500">{episode.description}</p>
        <div className="mt-auto flex flex-col gap-3 pt-3">
          {episode.audio_url ? (
            <audio controls preload="none" src={episode.audio_url} className="w-full" />
          ) : (
            <span className="w-fit rounded-full bg-neutral-100 px-3 py-1 text-xs text-neutral-500 dark:bg-neutral-800 dark:text-neutral-400">
              {t("card.soon")}
            </span>
          )}
          {onListened && !episode.listened && (
            <button
              onClick={onListened}
              className="w-fit text-sm font-medium text-brand-600 hover:text-brand-800 dark:text-brand-400 dark:hover:text-brand-300"
            >
              {t("card.markListened")}
            </button>
          )}
        </div>
      </div>
    </article>
  );
}
