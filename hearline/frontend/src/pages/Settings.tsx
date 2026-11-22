import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import {
  allows,
  api,
  type Frequency,
  type Interest,
  type Options,
  type Plan,
  type Profile,
  type TopicPref,
} from "../api";
import PodcastFeed from "../components/PodcastFeed";
import SubtopicInput from "../components/SubtopicInput";
import VoicePicker from "../components/VoicePicker";
import { useI18n } from "../i18n";

const FREQUENCIES: Frequency[] = ["daily", "weekdays", "weekly"];
const LENGTHS = [5, 10, 15, 20];

const pill = (active: boolean): string =>
  `rounded-full px-3 py-1 text-xs font-medium transition ${
    active
      ? "bg-neutral-900 text-white dark:bg-white dark:text-neutral-900"
      : "bg-white text-neutral-600 ring-1 ring-neutral-200 hover:ring-neutral-400 dark:bg-neutral-800 dark:text-neutral-300 dark:ring-neutral-700 dark:hover:ring-neutral-500"
  }`;

export default function Settings() {
  const { t, tone, name } = useI18n();
  const [interests, setInterests] = useState<Interest[]>([]);
  const [options, setOptions] = useState<Options | null>(null);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [status, setStatus] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [error, setError] = useState("");

  useEffect(() => {
    api.getInterests().then(setInterests);
    api.getOptions().then(setOptions);
    api.getProfile().then(setProfile);
    api.getPlans().then((p) => setPlans(p.plans));
  }, []);

  const plan = plans.find((p) => p.key === profile?.plan);
  if (!profile || !options || !plan) return <p className="text-neutral-500">{t("loading")}</p>;

  const update = (changes: Partial<Profile>) => {
    setProfile({ ...profile, ...changes });
    setStatus("idle");
  };
  const atTopicLimit = plan.max_topics !== null && profile.topics.length >= plan.max_topics;
  const lengths = LENGTHS.filter((m) => m <= plan.max_topic_minutes);
  const planName = plan.key === "free" ? t("subscription.free") : plan.label;
  const toggle = (slug: string) =>
    update({
      topics: profile.topics.some((t) => t.topic === slug)
        ? profile.topics.filter((t) => t.topic !== slug)
        : [...profile.topics, { topic: slug, frequency: "daily", target_minutes: Math.min(10, plan.max_topic_minutes) }],
    });
  const setPref = (slug: string, changes: Partial<TopicPref>) =>
    update({ topics: profile.topics.map((t) => (t.topic === slug ? { ...t, ...changes } : t)) });
  const save = () => {
    setStatus("saving");
    api
      .updateProfile(profile)
      .then((saved) => {
        setProfile(saved); // subcategories come back with their canonical names
        setStatus("saved");
      })
      .catch((e: Error) => {
        setError(e.message);
        setStatus("error");
      });
  };

  const selected = new Set(profile.topics.map((t) => t.topic));
  const bySlug = new Map(interests.map((i) => [i.slug, i]));

  return (
    <section className="space-y-12">
      <div>
        <h1 className="font-serif text-4xl font-medium tracking-tight">{t("settings.title")}</h1>
        <p className="mt-1 text-neutral-500">{t("settings.subtitle")}</p>
        <p className="mt-3 text-sm text-neutral-500">
          {plan.max_topics === null
            ? t("settings.planSummaryAll", { plan: planName, minutes: plan.max_topic_minutes })
            : t("settings.planSummary", { plan: planName, topics: plan.max_topics, minutes: plan.max_topic_minutes })}{" "}
          <Link to="/subscription" className="font-medium text-brand-600 hover:underline dark:text-brand-400">
            {t("settings.upgrade")}
          </Link>
        </p>
      </div>

      <div>
        <h2 className="text-lg font-semibold">{t("settings.style")}</h2>
        <p className="mb-4 text-sm text-neutral-500">{t("settings.styleHint")}</p>
        <div className="space-y-6 rounded-2xl border border-neutral-200 bg-white p-6 shadow-sm dark:border-neutral-800 dark:bg-neutral-900">
          <div>
            <span className="mb-2 block text-sm text-neutral-500">{t("settings.tone")}</span>
            <div className="flex flex-wrap gap-1.5">
              {options.tones.map(({ value, label }) => {
                const locked = !allows(plan.tones, value);
                return (
                  <button
                    key={value}
                    onClick={() => update({ tone: value })}
                    disabled={locked}
                    title={locked ? t("settings.locked") : undefined}
                    className={`${pill(profile.tone === value)} disabled:cursor-not-allowed disabled:opacity-40`}
                  >
                    {tone(value, label)}
                    {locked && " 🔒"}
                  </button>
                );
              })}
            </div>
          </div>
          <label className="block text-sm">
            <span className="mb-2 block text-neutral-500">{t("settings.language")}</span>
            <select
              value={profile.language}
              onChange={(e) => update({ language: e.target.value })}
              className="rounded-xl border border-neutral-200 bg-white px-3 py-2 outline-none focus:border-brand-500 dark:border-neutral-700 dark:bg-neutral-800"
            >
              {options.languages.map(({ value, label }) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <div>
            <span className="mb-2 block text-sm text-neutral-500">{t("settings.voice")}</span>
            <VoicePicker
              voices={options.voices}
              value={profile.voice}
              language={profile.language}
              onChange={(voice) => update({ voice })}
              allowed={(voice) => allows(plan.voices, voice)}
            />
          </div>
        </div>
      </div>

      <div>
        <h2 className="text-lg font-semibold">{t("settings.topics")}</h2>
        <p className="mb-4 text-sm text-neutral-500">
          {t("settings.topicsHint", { n: selected.size })}
        </p>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
          {interests.map(({ slug, label, labels, image }) => (
            <button
              key={slug}
              onClick={() => toggle(slug)}
              aria-pressed={selected.has(slug)}
              disabled={atTopicLimit && !selected.has(slug)}
              title={atTopicLimit && !selected.has(slug) ? t("settings.locked") : undefined}
              className={`group relative aspect-[4/3] overflow-hidden rounded-2xl text-left transition disabled:cursor-not-allowed disabled:opacity-40 ${
                selected.has(slug)
                  ? "ring-4 ring-brand-500"
                  : "ring-1 ring-neutral-200 hover:ring-neutral-400 dark:ring-neutral-700 dark:hover:ring-neutral-500"
              }`}
            >
              <img
                src={image}
                alt=""
                loading="lazy"
                referrerPolicy="no-referrer"
                className="h-full w-full object-cover transition group-hover:scale-105"
              />
              <div className="absolute inset-0 bg-gradient-to-t from-black/70 via-black/10 to-transparent" />
              <span className="absolute bottom-3 left-3 font-medium text-white">{name(label, labels)}</span>
              {selected.has(slug) && (
                <span className="absolute top-3 right-3 flex h-6 w-6 items-center justify-center rounded-full bg-brand-500 text-xs text-white">
                  ✓
                </span>
              )}
            </button>
          ))}
        </div>
      </div>

      {profile.topics.length > 0 && (
        <div>
          <h2 className="text-lg font-semibold">{t("settings.podcasts")}</h2>
          <p className="mb-4 text-sm text-neutral-500">
            {t("settings.podcastsHint")}
          </p>
          <ul className="divide-y divide-neutral-200 rounded-2xl border border-neutral-200 bg-white shadow-sm dark:divide-neutral-800 dark:border-neutral-800 dark:bg-neutral-900">
            {profile.topics.map((pref) => (
              <li key={pref.topic} className="flex flex-col gap-3 p-4 md:flex-row md:flex-wrap md:items-center">
                <div className="flex items-center gap-3 md:w-48">
                  <img
                    src={bySlug.get(pref.topic)?.image}
                    alt=""
                    referrerPolicy="no-referrer"
                    className="h-10 w-10 rounded-xl object-cover"
                  />
                  <span className="font-medium">
                    {name(bySlug.get(pref.topic)?.label ?? pref.topic, bySlug.get(pref.topic)?.labels)}
                  </span>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {FREQUENCIES.map((value) => (
                    <button
                      key={value}
                      onClick={() => setPref(pref.topic, { frequency: value })}
                      className={pill(pref.frequency === value)}
                    >
                      {t(`freq.${value}`)}
                    </button>
                  ))}
                </div>
                <div className="flex flex-wrap gap-1.5 md:ml-auto">
                  {lengths.map((minutes) => (
                    <button
                      key={minutes}
                      onClick={() => setPref(pref.topic, { target_minutes: minutes })}
                      className={pill(pref.target_minutes === minutes)}
                    >
                      {t("card.minutes", { n: minutes })}
                    </button>
                  ))}
                </div>
                <div className="md:basis-full">
                  {plan.subtopics ? (
                    <SubtopicInput
                      values={pref.subtopics ?? []}
                      suggestions={bySlug.get(pref.topic)?.suggestions}
                      translations={pref.subtopic_labels}
                      onChange={(subtopics) => setPref(pref.topic, { subtopics })}
                    />
                  ) : (
                    <p className="text-xs text-neutral-500">
                      🔒 {t("settings.subtopicsLocked")}{" "}
                      <Link to="/subscription" className="font-medium text-brand-600 hover:underline dark:text-brand-400">
                        {t("settings.upgrade")}
                      </Link>
                    </p>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex items-center gap-4">
        <button
          onClick={save}
          disabled={status === "saving"}
          className="rounded-full bg-brand-600 px-6 py-2.5 text-sm font-medium text-white transition hover:bg-brand-700 disabled:opacity-50"
        >
          {t("settings.save")}
        </button>
        {status === "saved" && <span className="text-sm text-green-600">{t("settings.saved")}</span>}
        {status === "error" && (
          <span className="text-sm text-red-600">{error || t("settings.saveError")}</span>
        )}
      </div>

      <PodcastFeed />
    </section>
  );
}
