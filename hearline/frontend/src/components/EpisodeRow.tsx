// A horizontal, scrollable row of episode tiles (streaming-app style). Click a tile to play it
// in the shared player; arrows page through the row on wider screens.
import { useRef, type ReactNode } from "react";
import { Link } from "react-router-dom";

import type { Episode } from "../api";
import { useI18n } from "../i18n";
import { usePlayer } from "../player";

interface Props {
  title: ReactNode;
  subtitle?: string;
  episodes: Episode[];
  seeAllTo?: string; // link shown next to the title
  large?: boolean; // bigger tiles, for the row at the top
  showTopic?: boolean; // topic name above the title (off in a row that is one topic)
  onListened?: (episode: Episode) => void;
}

export default function EpisodeRow({ title, subtitle, episodes, seeAllTo, large, showTopic = true, onListened }: Props) {
  const { t } = useI18n();
  const scroller = useRef<HTMLDivElement>(null);
  const page = (direction: 1 | -1) =>
    scroller.current?.scrollBy({ left: direction * scroller.current.clientWidth * 0.9, behavior: "smooth" });

  const arrow = (direction: 1 | -1) => (
    <button
      type="button"
      onClick={() => page(direction)}
      aria-label={t(direction === 1 ? "row.next" : "row.prev")}
      className={`absolute top-0 z-10 hidden h-full w-10 items-center justify-center text-2xl text-white opacity-0 transition group-hover/row:opacity-100 focus:opacity-100 md:flex ${
        direction === 1 ? "right-0 bg-gradient-to-l" : "left-0 bg-gradient-to-r"
      } from-black/50 to-transparent`}
    >
      {direction === 1 ? "›" : "‹"}
    </button>
  );

  return (
    <section>
      <div className="mb-3 flex items-baseline gap-3">
        <h2 className={`${large ? "text-2xl" : "text-lg"} font-semibold tracking-tight`}>{title}</h2>
        {seeAllTo && (
          <Link
            to={seeAllTo}
            className="text-sm font-medium text-brand-600 hover:text-brand-800 dark:text-brand-400 dark:hover:text-brand-300"
          >
            {t("home.seeAll")} ›
          </Link>
        )}
      </div>
      {subtitle && <p className="-mt-2 mb-3 text-sm text-neutral-500">{subtitle}</p>}
      <div className="group/row relative -mx-6">
        {arrow(-1)}
        <div
          ref={scroller}
          className="flex snap-x snap-mandatory scroll-px-6 gap-4 overflow-x-auto px-6 pb-2 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        >
          {episodes.map((ep) => (
            <EpisodeTile key={ep.id} episode={ep} large={large} showTopic={showTopic} onListened={onListened} />
          ))}
        </div>
        {arrow(1)}
      </div>
    </section>
  );
}

function EpisodeTile({
  episode,
  large,
  showTopic,
  onListened,
}: {
  episode: Episode;
  large?: boolean;
  showTopic: boolean;
  onListened?: (episode: Episode) => void;
}) {
  const { t, topicName } = useI18n();
  const { current, play } = usePlayer();
  const playing = current?.id === episode.id;
  const minutes =
    episode.duration_seconds === null
      ? null
      : t("card.minutes", { n: Math.max(1, Math.round(episode.duration_seconds / 60)) });

  return (
    <button
      type="button"
      disabled={!episode.audio_url}
      onClick={() => play(episode, onListened)}
      aria-label={t("voice.play", { name: episode.title })}
      className={`group shrink-0 snap-start text-left ${large ? "w-72 sm:w-96" : "w-56 sm:w-64"}`}
    >
      <div
        className={`relative aspect-video overflow-hidden rounded-xl bg-gradient-to-br from-brand-100 to-neutral-200 transition dark:from-brand-950 dark:to-neutral-800 ${
          playing ? "ring-4 ring-brand-500" : ""
        }`}
      >
        {episode.image_url && (
          <img
            src={episode.image_url}
            alt=""
            loading="lazy"
            referrerPolicy="no-referrer"
            className="h-full w-full object-cover transition duration-300 group-hover:scale-105 group-disabled:group-hover:scale-100"
          />
        )}
        <div className="absolute inset-0 bg-gradient-to-t from-black/60 via-transparent to-transparent" />
        {episode.audio_url ? (
          <span className="absolute inset-0 m-auto flex h-12 w-12 items-center justify-center rounded-full bg-white/90 text-neutral-900 opacity-0 shadow transition group-hover:opacity-100 group-focus-visible:opacity-100">
            {playing ? "♪" : "▶"}
          </span>
        ) : (
          <span className="absolute top-2 left-2 rounded-full bg-black/60 px-2 py-0.5 text-[11px] text-white">
            {t("card.soon")}
          </span>
        )}
        {episode.listened && (
          <span className="absolute top-2 right-2 rounded-full bg-black/60 px-2 py-0.5 text-[11px] text-white">
            ✓ {t("card.listened")}
          </span>
        )}
        {minutes && (
          <span className="absolute right-2 bottom-2 rounded bg-black/70 px-1.5 py-0.5 text-[11px] font-medium text-white">
            {minutes}
          </span>
        )}
      </div>
      {showTopic && (
        <p className="mt-2 text-xs font-medium text-brand-600 dark:text-brand-400">{topicName(episode)}</p>
      )}
      <h3 className={`${showTopic ? "mt-0.5" : "mt-2"} line-clamp-2 text-sm font-medium leading-snug`}>
        {episode.title}
      </h3>
    </button>
  );
}
