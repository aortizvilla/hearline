// The landing page for signed-out visitors: what Hearline does, the film, and the three steps to
// be in the loop. Start free / Sign in open a plan popup first (the app's real plans), then the
// sign-up or sign-in window. Topics, voices and plans all come from the public API.
import { useEffect, useRef, useState } from "react";

import { api, type Interest, type Options, type Plan, type Plans, type VoiceOption } from "../api";
import LanguageMenu from "../components/LanguageMenu";
import Logo from "../components/Logo";
import { useI18n } from "../i18n";
import { asset } from "../staticDemo";
import { PlanCard } from "./Subscription";

interface Props {
  onStart: () => void; // open sign-up (or the local sign-in, in development)
  onSignIn: () => void;
}

export const PLAN_KEY = "hearline.plan"; // the plan picked before signing up (App.tsx applies it)
const primary =
  "rounded-full bg-brand-600 px-6 py-3 text-base font-semibold text-white transition hover:bg-brand-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600 dark:bg-brand-400 dark:text-neutral-950 dark:hover:bg-brand-300";
const muted = "text-neutral-600 dark:text-neutral-400";

function Film() {
  const { t } = useI18n();
  const video = useRef<HTMLVideoElement>(null);
  const [sound, setSound] = useState(false);
  const reduced = typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const toggle = () => {
    const v = video.current;
    if (!v) return;
    if (!sound) {
      v.currentTime = 0;
      v.muted = false;
      v.play();
    } else v.muted = true;
    setSound(!sound);
  };
  return (
    <figure className="relative overflow-hidden rounded-lg bg-[#E6DDCC]">
      <video
        ref={video}
        className="block aspect-video w-full"
        src={asset("/media/hearline.mp4")}
        poster={asset("/media/hearline-poster.jpg")}
        autoPlay={!reduced}
        muted
        loop
        playsInline
        preload="metadata"
        aria-label={t("landing.filmLabel")}
      />
      <button
        type="button"
        onClick={toggle}
        className="absolute right-3 top-3 flex items-center gap-2 rounded-full bg-white/90 px-4 py-2 text-sm font-medium text-neutral-900 shadow-sm backdrop-blur transition hover:bg-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-600"
      >
        <span aria-hidden className={sound ? "text-accent" : ""}>{sound ? "■" : "▶"}</span>
        {sound ? t("landing.mute") : t("landing.sound")}
      </button>
    </figure>
  );
}

// Step 1: every topic as a tile; tap one to see its subjects
function Topics({ topics }: { topics: Interest[] }) {
  const { t, name } = useI18n();
  const [all, setAll] = useState(false);
  const [open, setOpen] = useState<string | null>(topics[0]?.slug ?? null);
  const shown = all ? topics : topics.slice(0, 12);
  const current = topics.find((x) => x.slug === open);
  return (
    <div className="mt-5">
      <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-6">
        {shown.map((x) => (
          <button
            key={x.slug}
            type="button"
            onClick={() => setOpen(x.slug)}
            aria-pressed={open === x.slug}
            className={`group relative aspect-[4/3] overflow-hidden rounded-lg text-left transition ${
              open === x.slug ? "ring-2 ring-brand-600 dark:ring-brand-400" : "ring-1 ring-neutral-200 hover:ring-neutral-400 dark:ring-neutral-800"
            }`}
          >
            <img src={x.image} alt="" loading="lazy" referrerPolicy="no-referrer" className="h-full w-full object-cover" />
            <span className="absolute inset-0 bg-gradient-to-t from-black/70 to-transparent" />
            <span className="absolute bottom-1.5 left-2 right-2 truncate text-xs font-medium text-white">{name(x.label, x.labels)}</span>
          </button>
        ))}
      </div>
      {topics.length > 12 && (
        <button type="button" onClick={() => setAll(!all)} className="mt-3 text-sm font-medium text-brand-700 hover:underline dark:text-brand-300">
          {all ? t("landing.fewerTopics") : t("landing.moreTopics", { n: topics.length })}
        </button>
      )}
      {current && (current.suggestions?.length ?? 0) > 0 && (
        <div className="mt-4">
          <p className={`text-sm ${muted}`}>{t("landing.subjects", { topic: name(current.label, current.labels) })}</p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {current.suggestions!.map((s) => (
              <span key={s.label} className="rounded-full bg-brand-50 px-3 py-1 text-sm text-brand-800 dark:bg-brand-500/15 dark:text-brand-200">
                {name(s.label, s.labels)}
              </span>
            ))}
            <span className={`rounded-full px-3 py-1 text-sm ring-1 ring-dashed ring-neutral-300 dark:ring-neutral-700 ${muted}`}>
              {t("landing.yourOwn")}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}

// Step 2: pick a language, play each voice's real sample in it
function Voices({ options }: { options: Options }) {
  const { t, tone, catalog } = useI18n();
  const [lang, setLang] = useState("en");
  const [playing, setPlaying] = useState<string | null>(null);
  const audio = useRef<HTMLAudioElement | null>(null);
  useEffect(() => () => audio.current?.pause(), []);
  const play = (v: VoiceOption) => {
    const url = v.previews[lang] ?? v.previews.en;
    audio.current?.pause();
    if (!url || playing === v.value) return setPlaying(null);
    const a = new Audio(url);
    audio.current = a;
    a.onended = () => setPlaying(null);
    setPlaying(v.value);
    a.play().catch(() => setPlaying(null));
  };
  return (
    <div className="mt-5 space-y-4">
      <div className="flex flex-wrap gap-1.5" role="group" aria-label={t("landing.language")}>
        {options.languages.map((l) => (
          <button
            key={l.value}
            type="button"
            onClick={() => { audio.current?.pause(); setPlaying(null); setLang(l.value); }}
            aria-pressed={lang === l.value}
            className={`rounded-full px-3 py-1 text-sm transition ${
              lang === l.value ? "bg-neutral-900 text-white dark:bg-white dark:text-neutral-900" : "ring-1 ring-neutral-300 hover:ring-neutral-500 dark:ring-neutral-700"
            }`}
          >
            {l.label}
          </button>
        ))}
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {options.voices.map((v) => (
          <button
            key={v.value}
            type="button"
            onClick={() => play(v)}
            className="flex items-center gap-3 rounded-lg bg-white px-3 py-2.5 text-left ring-1 ring-neutral-200 transition hover:ring-brand-600 dark:bg-neutral-900 dark:ring-neutral-800"
          >
            <span aria-hidden className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-xs ${playing === v.value ? "bg-accent text-white" : "bg-brand-50 text-brand-800 dark:bg-brand-500/15 dark:text-brand-200"}`}>
              {playing === v.value ? "■" : "▶"}
            </span>
            <span className="min-w-0">
              <span className="block font-medium">{v.label}</span>
              <span className={`block truncate text-xs ${muted}`}>{catalog(v.accents[lang] || v.accent)}</span>
            </span>
          </button>
        ))}
      </div>
      <p className={`text-sm ${muted}`}>
        {t("landing.tones")} {options.tones.map((o) => tone(o.value, o.label).toLowerCase()).join(", ")}.
      </p>
    </div>
  );
}

// The popup behind Start free / Sign in: the plans, then sign-up or sign-in
function PlanDialog({ plans, onClose, onContinue, onSignIn }: {
  plans: Plans;
  onClose: () => void;
  onContinue: (plan: Plan) => void;
  onSignIn: () => void;
}) {
  const { t } = useI18n();
  const [picked, setPicked] = useState(plans.plans[0]?.key ?? "free");
  const plan = plans.plans.find((p) => p.key === picked) ?? plans.plans[0];
  const planName = (p: Plan) => (p.key === "free" ? t("subscription.free") : p.label);
  useEffect(() => {
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-4 sm:items-center" onClick={onClose}>
      <div role="dialog" aria-modal="true" aria-labelledby="plans-title" onClick={(e) => e.stopPropagation()}
        className="w-full max-w-4xl rounded-2xl bg-neutral-50 p-6 shadow-xl sm:p-8 dark:bg-neutral-950">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 id="plans-title" className="font-serif text-3xl font-medium">{t("landing.plansTitle")}</h2>
            <p className={`mt-1 ${muted}`}>{plans.self_serve ? t("landing.plansBody") : t("landing.paidSoon")}</p>
          </div>
          <button type="button" onClick={onClose} aria-label={t("landing.close")} className="rounded-full p-2 text-xl leading-none hover:bg-black/5 dark:hover:bg-white/10">×</button>
        </div>
        <div className="mt-6 grid gap-4 md:grid-cols-3">
          {plans.plans.map((p) => (
            <PlanCard key={p.key} plan={p} current={p.key === picked} selfServe={plans.self_serve || p.key === "free"}
              busy={false} onChoose={() => setPicked(p.key)} currentLabel={t("landing.selected")} actionLabel={t("landing.choose")} />
          ))}
        </div>
        <div className="mt-6 flex flex-col items-center gap-3 sm:flex-row sm:justify-between">
          <button type="button" onClick={onSignIn} className={`text-sm ${muted} hover:underline`}>
            {t("landing.haveAccount")} <span className="font-medium text-brand-700 dark:text-brand-300">{t("auth.signIn")}</span>
          </button>
          <button type="button" onClick={() => onContinue(plan)} className={primary}>
            {t("landing.continue", { plan: planName(plan) })}
          </button>
        </div>
      </div>
    </div>
  );
}

// The landing page follows the browser's light / dark setting, live. Signed-in pages keep the
// user's own choice (theme.ts); leaving the landing page restores it.
function useBrowserTheme() {
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)"), root = document.documentElement;
    const before = root.classList.contains("dark");
    const apply = () => root.classList.toggle("dark", mq.matches);
    apply();
    mq.addEventListener("change", apply);
    return () => { mq.removeEventListener("change", apply); root.classList.toggle("dark", before); };
  }, []);
}

export default function Landing({ onStart, onSignIn }: Props) {
  useBrowserTheme();
  const { t } = useI18n();
  const [options, setOptions] = useState<Options | null>(null);
  const [topics, setTopics] = useState<Interest[]>([]);
  const [plans, setPlans] = useState<Plans | null>(null);
  const [dialog, setDialog] = useState(false);
  useEffect(() => {
    api.getOptions().then(setOptions).catch(() => setOptions(null));
    api.getInterests().then(setTopics).catch(() => setTopics([]));
    api.getPlans().then(setPlans).catch(() => setPlans(null));
  }, []);
  const open = () => (plans ? setDialog(true) : onStart());   // no plans loaded: go straight on
  const go = (next: () => void, plan?: Plan) => {
    try { if (plan) localStorage.setItem(PLAN_KEY, plan.key); } catch { /* storage blocked: start on the default plan */ }
    setDialog(false);
    next();
  };

  const steps = [
    { title: t("landing.step1"), body: t("landing.step1Body") },
    { title: t("landing.step2"), body: t("landing.step2Body") },
    { title: t("landing.step3"), body: t("landing.step3Body") },
  ];
  return (
    <div className="min-h-screen bg-neutral-50 text-neutral-900 antialiased dark:bg-neutral-950 dark:text-neutral-100">
      <header className="relative z-30 mx-auto flex max-w-5xl items-center justify-between px-4 py-5 sm:px-6">
        <Logo />
        <nav className="flex items-center gap-2">
          <LanguageMenu />
          <button type="button" onClick={open} className="rounded-full px-4 py-2 text-sm font-medium hover:bg-black/5 dark:hover:bg-white/10">
            {t("auth.signIn")}
          </button>
        </nav>
      </header>

      <main className="mx-auto max-w-5xl px-4 pb-24 sm:px-6">
        {/* the product first: what it is, and how to start */}
        <section className="grid gap-8 pt-6 md:grid-cols-[1fr_auto] md:items-end">
          <div className="max-w-2xl">
            <h1 className="font-serif text-4xl font-medium leading-[1.1] sm:text-5xl">{t("landing.title")}</h1>
            <p className={`mt-4 text-lg leading-relaxed ${muted}`}>{t("landing.subtitle")}</p>
          </div>
          <div className="flex flex-col items-start gap-2 md:items-end">
            <button type="button" onClick={open} className={primary}>{t("landing.start")}</button>
            <span className={`text-sm ${muted}`}>{t("landing.free")}</span>
          </div>
        </section>

        {/* then the film */}
        <section className="mt-10">
          <Film />
        </section>

        {/* the three steps: a real sequence, so numbered */}
        <section className="mt-20" aria-labelledby="steps">
          <h2 id="steps" className="font-serif text-3xl font-medium">{t("landing.stepsTitle")}</h2>
          <ol className="mt-8 space-y-10">
            {steps.map((s, i) => (
              <li key={s.title} className="grid gap-4 border-t border-neutral-200 pt-6 sm:grid-cols-[4rem_1fr] dark:border-neutral-800">
                <span className="font-serif text-4xl leading-none text-brand-600 dark:text-brand-400">{i + 1}</span>
                <div className="min-w-0">
                  <h3 className="text-xl font-semibold">{s.title}</h3>
                  <p className={`mt-2 max-w-2xl leading-relaxed ${muted}`}>{s.body}</p>
                  {i === 0 && topics.length > 0 && <Topics topics={topics} />}
                  {i === 1 && options && <Voices options={options} />}
                </div>
              </li>
            ))}
          </ol>
        </section>

        {/* the ending, as in the film: tomorrow's briefing waits in an empty slot */}
        <section className="mt-20 rounded-lg border-2 border-dashed border-brand-300 px-6 py-10 text-center dark:border-brand-800">
          <p className="font-serif text-3xl font-medium">{t("landing.endTitle")}</p>
          <p className={`mx-auto mt-3 max-w-md ${muted}`}>{t("landing.endBody")}</p>
          <button type="button" onClick={open} className={`${primary} mt-6`}>{t("landing.start")}</button>
        </section>
      </main>

      {dialog && plans && (
        <PlanDialog plans={plans} onClose={() => setDialog(false)} onContinue={(p) => go(onStart, p)} onSignIn={() => go(onSignIn)} />
      )}
    </div>
  );
}
