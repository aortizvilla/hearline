// One shared player: any tile can start an episode, and it keeps playing in a bar at the bottom
// while you browse. Finishing an episode (or pressing ✓) marks it as listened. Progress is
// reported every 15 s, on pause and at the end (start and completion rates), and playback
// resumes where the listener stopped.
import { createContext, useContext, useRef, useState, type ReactNode } from "react";

import { api, type Episode } from "./api";
import { useI18n } from "./i18n";

type OnListened = (episode: Episode) => void;

const REPORT_EVERY = 15; // seconds of playback between progress reports

interface Player {
  current: Episode | null;
  play: (episode: Episode, onListened?: OnListened) => void;
}

const PlayerContext = createContext<Player | null>(null);

export function PlayerProvider({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const [current, setCurrent] = useState<Episode | null>(null);
  const [onListened, setOnListened] = useState<OnListened | undefined>(undefined);
  const reported = useRef(0); // playback position last sent

  const play = (episode: Episode, callback?: OnListened) => {
    reported.current = episode.progress_seconds;
    setCurrent(episode);
    setOnListened(() => callback);
  };
  const report = (seconds: number, force = false) => {
    if (!current || (!force && seconds - reported.current < REPORT_EVERY)) return;
    reported.current = seconds;
    api.saveProgress(current.id, seconds).catch(() => undefined); // best effort
  };
  const resume = (audio: HTMLAudioElement) => {
    const at = current?.progress_seconds ?? 0;
    if (at > 0 && at < audio.duration - 10) audio.currentTime = at; // not if nearly done
  };
  const markListened = () => {
    if (!current || current.listened) return;
    api.markListened(current.id).then((updated) => {
      setCurrent((c) => (c?.id === updated.id ? updated : c));
      onListened?.(updated);
    });
  };

  return (
    <PlayerContext.Provider value={{ current, play }}>
      {children}
      {current?.audio_url && (
        <>
          <div className="h-28" aria-hidden />
          <div className="fixed inset-x-0 bottom-0 z-20 border-t border-neutral-200 bg-white/95 backdrop-blur dark:border-neutral-800 dark:bg-neutral-950/95">
            <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-x-4 gap-y-2 px-6 py-3 sm:flex-nowrap">
              <div className="flex min-w-0 flex-1 items-center gap-3 sm:w-64 sm:flex-none">
                {current.image_url && (
                  <img
                    src={current.image_url}
                    alt=""
                    referrerPolicy="no-referrer"
                    className="h-12 w-12 shrink-0 rounded-lg object-cover"
                  />
                )}
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{current.title}</p>
                  <p className="truncate text-xs text-neutral-500">{current.topic_label}</p>
                </div>
              </div>
              <audio
                key={current.id}
                controls
                autoPlay
                src={current.audio_url}
                onLoadedMetadata={(e) => resume(e.currentTarget)}
                onTimeUpdate={(e) => report(e.currentTarget.currentTime)}
                onPause={(e) => report(e.currentTarget.currentTime, true)}
                onEnded={(e) => {
                  report(e.currentTarget.duration, true);
                  markListened();
                }}
                className="order-last w-full sm:order-none sm:flex-1"
              />
              {!current.listened && (
                <button
                  onClick={markListened}
                  title={t("card.markListened")}
                  aria-label={t("card.markListened")}
                  className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-neutral-500 ring-1 ring-neutral-200 transition hover:text-brand-600 hover:ring-brand-500 dark:ring-neutral-700"
                >
                  ✓
                </button>
              )}
              <button
                onClick={() => setCurrent(null)}
                title={t("player.close")}
                aria-label={t("player.close")}
                className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-neutral-500 transition hover:bg-neutral-100 hover:text-neutral-900 dark:hover:bg-neutral-800 dark:hover:text-white"
              >
                ✕
              </button>
            </div>
          </div>
        </>
      )}
    </PlayerContext.Provider>
  );
}

export function usePlayer(): Player {
  const ctx = useContext(PlayerContext);
  if (!ctx) throw new Error("usePlayer must be used inside PlayerProvider");
  return ctx;
}
