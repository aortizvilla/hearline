// "Listen in your podcast app": the listener's private feed link (backend app/api/feed.py).
// Apple Podcasts, Pocket Casts, Overcast... accept it; Spotify does not take feed links.
import { useEffect, useState } from "react";

import { api, type PodcastLinks } from "../api";
import { useI18n } from "../i18n";

export default function PodcastFeed() {
  const { t } = useI18n();
  const [links, setLinks] = useState<PodcastLinks | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    api.getPodcast().then(setLinks).catch(() => setLinks(null));
  }, []);

  if (!links) return null;

  const copy = () =>
    navigator.clipboard.writeText(links.feed_url).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    });
  const reset = () => {
    if (window.confirm(t("podcast.resetConfirm"))) api.resetPodcast().then(setLinks);
  };

  return (
    <div>
      <h2 className="text-lg font-semibold">{t("podcast.title")}</h2>
      <p className="mb-4 text-sm text-neutral-500">{t("podcast.hint")}</p>
      <div className="space-y-3 rounded-2xl border border-neutral-200 bg-white p-6 shadow-sm dark:border-neutral-800 dark:bg-neutral-900">
        <div className="flex flex-col gap-2 sm:flex-row">
          <input
            readOnly
            value={links.feed_url}
            onFocus={(e) => e.currentTarget.select()}
            aria-label={t("podcast.title")}
            className="min-w-0 flex-1 rounded-xl border border-neutral-200 bg-neutral-50 px-3 py-2 font-mono text-xs dark:border-neutral-700 dark:bg-neutral-800"
          />
          <button
            type="button"
            onClick={copy}
            className="rounded-full bg-brand-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-brand-700"
          >
            {copied ? t("podcast.copied") : t("podcast.copy")}
          </button>
        </div>
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <a
            href={links.apple_podcasts_url}
            className="rounded-full px-3 py-1.5 font-medium text-brand-600 ring-1 ring-brand-200 hover:bg-brand-50 dark:text-brand-300 dark:ring-brand-500/40 dark:hover:bg-brand-500/10"
          >
            {t("podcast.apple")}
          </a>
          <button
            type="button"
            onClick={reset}
            className="rounded-full px-3 py-1.5 text-neutral-500 ring-1 ring-neutral-200 hover:text-neutral-900 dark:ring-neutral-700 dark:hover:text-white"
          >
            {t("podcast.reset")}
          </button>
        </div>
        <p className="text-xs text-neutral-500">{t("podcast.spotify")}</p>
      </div>
    </div>
  );
}
