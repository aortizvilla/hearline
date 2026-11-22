// Subscription page: the account (photo, name, email, sign out) and the plans.
// There is no billing yet: users switch plan here only while self-serve is on
// (always in local development); otherwise an admin moves them.
import { useClerk } from "@clerk/clerk-react";
import { useEffect, useState } from "react";

import { api, setDevUser, type Plan, type Plans, type Profile } from "../api";
import { AccountAvatar, SessionControl, authEnabled } from "../auth";
import { useI18n } from "../i18n";

const card =
  "rounded-2xl border border-neutral-200 bg-white p-6 shadow-sm dark:border-neutral-800 dark:bg-neutral-900";

function Field({ label, ...props }: { label: string } & React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block text-neutral-500">{label}</span>
      <input
        {...props}
        className="w-full rounded-xl border border-neutral-200 bg-white px-3 py-2 outline-none focus:border-brand-500 disabled:cursor-not-allowed disabled:opacity-70 dark:border-neutral-700 dark:bg-neutral-800"
      />
    </label>
  );
}

// Shared with the landing page's plan popup: currentLabel / actionLabel override the wording there
export function PlanCard({ plan, current, selfServe, busy, onChoose, currentLabel, actionLabel }: {
  plan: Plan;
  current: boolean;
  selfServe: boolean;
  busy: boolean;
  onChoose: () => void;
  currentLabel?: string;
  actionLabel?: string;
}) {
  const { t } = useI18n();
  const name = plan.key === "free" ? t("subscription.free") : plan.label;
  // [text, included]: what's not included is listed with a cross
  const features: [string, boolean][] = [
    [plan.max_topics === null ? t("subscription.allTopics") : t("subscription.topics", { n: plan.max_topics }), true],
    [t("subscription.topicMinutes", { n: plan.max_topic_minutes }), true],
    [t("subscription.episodeMinutes", { n: plan.max_episode_minutes }), true],
    [plan.voices === "all" ? t("subscription.allVoices") : t("subscription.voices", { n: plan.voices.length }), true],
    plan.tones === "all" ? [t("subscription.allTones"), true] : [t("subscription.noTones"), false],
    plan.subtopics ? [t("subscription.subtopics"), true] : [t("subscription.noSubtopics"), false],
  ];
  return (
    <div className={`${card} flex flex-col ${current ? "ring-2 ring-brand-500" : ""}`}>
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="text-xl font-semibold">{name}</h3>
        {current && (
          <span className="rounded-full bg-brand-50 px-2.5 py-0.5 text-xs font-medium text-brand-700 dark:bg-brand-500/15 dark:text-brand-300">
            {currentLabel ?? t("subscription.current")}
          </span>
        )}
      </div>
      <p className="mt-2">
        <span className="text-3xl font-semibold">${plan.price_usd_month.toFixed(plan.price_usd_month ? 2 : 0)}</span>
        <span className="text-sm text-neutral-500">{t("subscription.perMonth")}</span>
      </p>
      <ul className="mt-4 flex-1 space-y-2 text-sm">
        {features.map(([text, included]) => (
          <li key={text} className={`flex gap-2 ${included ? "" : "text-neutral-400 dark:text-neutral-500"}`}>
            <span aria-hidden className={included ? "text-brand-500" : "text-neutral-400"}>
              {included ? "✓" : "✗"}
            </span>
            {text}
          </li>
        ))}
      </ul>
      {!current && selfServe && (
        <button
          onClick={onChoose}
          disabled={busy}
          className="mt-6 rounded-full bg-brand-600 px-5 py-2 text-sm font-medium text-white transition hover:bg-brand-700 disabled:opacity-50"
        >
          {actionLabel ?? t("subscription.choose", { plan: name })}
        </button>
      )}
    </div>
  );
}

// With Clerk the login account owns the name and email: shown read-only, changed in Clerk
function ManageAccount() {
  const { t } = useI18n();
  const clerk = useClerk();
  return (
    <button type="button" onClick={() => clerk.openUserProfile()} className="text-sm font-medium text-brand-700 hover:underline dark:text-brand-300">
      {t("account.manage")}
    </button>
  );
}

// Deleting the account: two steps, then the account (and, with Clerk, the login) is gone
function DeleteAccount() {
  const { t } = useI18n();
  const [step, setStep] = useState<"idle" | "confirm" | "busy" | "error">("idle");
  const finish = async () => {
    setStep("busy");
    try {
      await api.deleteAccount();
      setDevUser(null);
      window.location.assign(import.meta.env.BASE_URL);
    } catch {
      setStep("error");
    }
  };
  return <DeleteAccountView step={step} onAsk={() => setStep("confirm")} onCancel={() => setStep("idle")} onConfirm={finish} t={t} />;
}

function ClerkDeleteAccount() {
  const { t } = useI18n();
  const clerk = useClerk();
  const [step, setStep] = useState<"idle" | "confirm" | "busy" | "error">("idle");
  const finish = async () => {
    setStep("busy");
    try {
      await api.deleteAccount();                          // Hearline's data first
      await clerk.user?.delete().catch(() => undefined);  // then the login (if Clerk allows it)
      await clerk.signOut({ redirectUrl: "/" });
    } catch {
      setStep("error");
    }
  };
  return <DeleteAccountView step={step} onAsk={() => setStep("confirm")} onCancel={() => setStep("idle")} onConfirm={finish} t={t} />;
}

function DeleteAccountView({ step, onAsk, onCancel, onConfirm, t }: {
  step: "idle" | "confirm" | "busy" | "error";
  onAsk: () => void;
  onCancel: () => void;
  onConfirm: () => void;
  t: ReturnType<typeof useI18n>["t"];
}) {
  return (
    <div className={`${card} border-red-200 dark:border-red-900/60`}>
      <h3 className="font-semibold">{t("account.deleteTitle")}</h3>
      <p className="mt-1 text-sm text-neutral-500">{t("account.deleteBody")}</p>
      {step === "idle" || step === "error" ? (
        <button type="button" onClick={onAsk} className="mt-4 rounded-full px-5 py-2 text-sm font-medium text-red-600 ring-1 ring-red-200 transition hover:bg-red-50 dark:text-red-400 dark:ring-red-900 dark:hover:bg-red-950">
          {t("account.delete")}
        </button>
      ) : (
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <span className="text-sm font-medium">{t("account.deleteConfirm")}</span>
          <button type="button" onClick={onConfirm} disabled={step === "busy"} className="rounded-full bg-red-600 px-5 py-2 text-sm font-medium text-white transition hover:bg-red-700 disabled:opacity-50">
            {t("account.deleteYes")}
          </button>
          <button type="button" onClick={onCancel} disabled={step === "busy"} className="rounded-full px-4 py-2 text-sm hover:bg-black/5 dark:hover:bg-white/10">
            {t("account.cancel")}
          </button>
        </div>
      )}
      {step === "error" && <p className="mt-2 text-sm text-red-600">{t("account.deleteError")}</p>}
    </div>
  );
}

export default function Subscription() {
  const { t } = useI18n();
  const [profile, setProfile] = useState<Profile | null>(null);
  const [plans, setPlans] = useState<Plans | null>(null);
  const [status, setStatus] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [planStatus, setPlanStatus] = useState<{ busy: boolean; message: string; error: boolean }>({
    busy: false,
    message: "",
    error: false,
  });

  useEffect(() => {
    api.getProfile().then(setProfile);
    api.getPlans().then(setPlans);
  }, []);

  const choose = (plan: Plan) => {
    setPlanStatus({ busy: true, message: "", error: false });
    api
      .changePlan(plan.key)
      .then((saved) => {
        setProfile(saved); // a downgrade may have trimmed topics, voice or tone
        const name = plan.key === "free" ? t("subscription.free") : plan.label;
        setPlanStatus({ busy: false, message: t("subscription.changed", { plan: name }), error: false });
      })
      .catch((e: Error) =>
        setPlanStatus({ busy: false, message: e.message || t("subscription.changeError"), error: true }),
      );
  };

  const update = (changes: Partial<Profile>) => {
    setProfile((p) => (p ? { ...p, ...changes } : p));
    setStatus("idle");
  };
  const save = () => {
    if (!profile) return;
    setStatus("saving");
    api
      .updateProfile(profile)
      .then(() => setStatus("saved"))
      .catch(() => setStatus("error"));
  };

  return (
    <section className="space-y-10">
      <div>
        <h1 className="font-serif text-4xl font-medium tracking-tight">{t("menu.subscription")}</h1>
        <p className="mt-1 text-neutral-500">{t("subscription.subtitle")}</p>
      </div>

      <div>
        <h2 className="mb-4 text-lg font-semibold">{t("subscription.account")}</h2>
        {!profile ? (
          <p className="text-neutral-500">{t("loading")}</p>
        ) : (
          <div className={`${card} space-y-6`}>
            <div className="flex flex-col gap-6 sm:flex-row sm:items-center">
              <AccountAvatar name={profile.name} size="h-20 w-20 shrink-0 text-2xl" />
              <div className="grid flex-1 gap-4 sm:grid-cols-2">
                <Field label={t("settings.name")} value={profile.name} disabled={authEnabled} onChange={(e) => update({ name: e.target.value })} />
                <Field
                  label={t("settings.email")}
                  type="email"
                  value={profile.email}
                  disabled={authEnabled}
                  placeholder="you@example.com"
                  onChange={(e) => update({ email: e.target.value })}
                />
              </div>
            </div>
            {authEnabled && (
              <p className="flex flex-wrap items-center gap-x-3 text-sm text-neutral-500">
                {t("account.fromLogin")} <ManageAccount />
              </p>
            )}
            <div className="flex flex-wrap items-center gap-4 border-t border-neutral-200 pt-5 dark:border-neutral-800">
              {!authEnabled && (
              <button
                onClick={save}
                disabled={status === "saving"}
                className="rounded-full bg-brand-600 px-5 py-2 text-sm font-medium text-white transition hover:bg-brand-700 disabled:opacity-50"
              >
                {t("settings.save")}
              </button>
              )}
              {status === "saved" && <span className="text-sm text-green-600">{t("settings.saved")}</span>}
              {status === "error" && <span className="text-sm text-red-600">{t("settings.saveError")}</span>}
              <div className="sm:ml-auto">
                <SessionControl />
              </div>
            </div>
          </div>
        )}
      </div>

      <div>
        <h2 className="mb-4 text-lg font-semibold">{t("subscription.planTitle")}</h2>
        {!plans || !profile ? (
          <p className="text-neutral-500">{t("loading")}</p>
        ) : (
          <>
            <div className="grid gap-4 md:grid-cols-3">
              {plans.plans.map((plan) => (
                <PlanCard
                  key={plan.key}
                  plan={plan}
                  current={plan.key === profile.plan}
                  selfServe={plans.self_serve}
                  busy={planStatus.busy}
                  onChoose={() => choose(plan)}
                />
              ))}
            </div>
            <p className="mt-4 text-sm text-neutral-500">
              {plans.self_serve ? t("subscription.trial") : t("subscription.soon")}
            </p>
            {planStatus.message && (
              <p className={`mt-2 text-sm ${planStatus.error ? "text-red-600" : "text-green-600"}`}>
                {planStatus.message}
              </p>
            )}
          </>
        )}
      </div>

      <div>
        <h2 className="mb-4 text-lg font-semibold">{t("account.dataTitle")}</h2>
        {authEnabled ? <ClerkDeleteAccount /> : <DeleteAccount />}
      </div>
    </section>
  );
}
