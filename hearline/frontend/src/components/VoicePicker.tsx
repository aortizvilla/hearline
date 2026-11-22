// Voice cards for Settings: pick a voice, and play its short sample in the language selected
// in the form (English if that language has no sample yet) before choosing.
import { useEffect, useRef, useState } from "react";

import type { VoiceOption } from "../api";
import { useI18n } from "../i18n";

interface Props {
  voices: VoiceOption[];
  value: string;
  language: string;
  onChange: (voice: string) => void;
  allowed?: (voice: string) => boolean; // the listener's plan; others are shown locked
}

export default function VoicePicker({ voices, value, language, onChange, allowed = () => true }: Props) {
  const { t, catalog } = useI18n();
  const audio = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState<string | null>(null);

  useEffect(() => () => audio.current?.pause(), []); // stop when leaving the page
  useEffect(() => {
    audio.current?.pause(); // a new language: the sample playing is the old one
    setPlaying(null);
  }, [language]);

  const sampleOf = (voice: VoiceOption): string | undefined =>
    voice.previews[language] ?? voice.previews.en;

  const toggle = (voice: VoiceOption) => {
    const url = sampleOf(voice);
    if (!url) return;
    audio.current?.pause();
    if (playing === voice.value) {
      setPlaying(null);
      return;
    }
    const sample = new Audio(url);
    audio.current = sample;
    sample.onended = () => setPlaying(null);
    setPlaying(voice.value);
    sample.play().catch(() => setPlaying(null));
  };

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-3">
      {voices.map((voice) => {
        const selected = value === voice.value;
        const locked = !allowed(voice.value);
        const accent = voice.accents[language] ?? (language === "en" ? voice.accent : "");
        const details = [accent, voice.gender]
          .map(catalog)
          .filter(Boolean)
          .join(" · ");
        return (
          <div
            key={voice.value}
            className={`flex items-center gap-2 rounded-xl p-3 transition ${
              selected
                ? "bg-brand-50 ring-2 ring-brand-500 dark:bg-brand-500/15"
                : "ring-1 ring-neutral-200 hover:ring-neutral-400 dark:ring-neutral-700 dark:hover:ring-neutral-500"
            }`}
          >
            <button
              type="button"
              onClick={() => onChange(voice.value)}
              aria-pressed={selected}
              disabled={locked}
              title={locked ? t("settings.locked") : undefined}
              className="min-w-0 flex-1 text-left disabled:cursor-not-allowed disabled:opacity-50"
            >
              <span className="block font-medium">
                {voice.label}
                {locked && <span aria-label={t("settings.locked")}> 🔒</span>}
              </span>
              <span className="block text-xs text-neutral-500">{catalog(voice.description)}</span>
              {details && (
                <span className="mt-1 block text-[11px] uppercase tracking-wide text-neutral-400">
                  {details}
                </span>
              )}
            </button>
            {sampleOf(voice) && (
              <button
                type="button"
                onClick={() => toggle(voice)}
                aria-label={t(playing === voice.value ? "voice.stop" : "voice.play", { name: voice.label })}
                title={t(playing === voice.value ? "voice.stopShort" : "voice.listen")}
                className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-white text-sm text-neutral-700 ring-1 ring-neutral-200 transition hover:ring-brand-500 dark:bg-neutral-800 dark:text-neutral-200 dark:ring-neutral-700"
              >
                {playing === voice.value ? "■" : "▶"}
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}
