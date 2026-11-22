// Login with Clerk. Set VITE_CLERK_PUBLISHABLE_KEY (hearline/frontend/.env) to turn it on; without it
// the app runs in dev mode and the backend uses its single default user (AUTH_MODE=dev).
import {
  ClerkProvider,
  SignedIn,
  SignedOut,
  useAuth,
  useClerk,
  useUser,
} from "@clerk/clerk-react";
import { useEffect, useState, type ReactNode } from "react";

import { api, getDevUser, setDevUser, setTokenGetter } from "./api";
import AvatarMenu, { Avatar } from "./components/AvatarMenu";
import { useI18n } from "./i18n";
import Landing from "./pages/Landing";
import Logo from "./components/Logo";

const publishableKey = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY as string | undefined;
export const authEnabled = Boolean(publishableKey);

// Hands Clerk's session-token getter to the API client. Set during render (not in an effect)
// so it is in place before any page starts fetching.
function TokenBridge() {
  const { getToken } = useAuth();
  setTokenGetter(() => getToken());
  return null;
}

const primaryButton =
  "rounded-full bg-brand-600 px-6 py-2.5 text-sm font-medium text-white transition hover:bg-brand-700";

// Local development without Clerk: sign in as a user by email (no password), or continue as
// the default user. The backend only accepts this when AUTH_MODE=dev. Shown in a dialog over
// the landing page.
function DevSignIn({ onDone, onClose }: { onDone: () => void; onClose: () => void }) {
  const { t } = useI18n();
  const [email, setEmail] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    api
      .devLogin(email)
      .then((u) => {
        setDevUser(String(u.user_id));
        onDone();
      })
      .catch((err: Error) => setError(err.message));
  };
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <form
        role="dialog"
        aria-modal="true"
        aria-label={t("auth.signIn")}
        onSubmit={submit}
        className="flex w-full max-w-sm flex-col items-center gap-5 rounded-2xl bg-white p-8 text-center text-neutral-900 shadow-xl dark:bg-neutral-900 dark:text-neutral-100"
      >
        <span className="font-serif text-4xl font-medium tracking-tight">
          <Logo size="lg" />
        </span>
        <input
          type="email"
          required
          autoFocus
          value={email}
          onChange={(e) => {
            setEmail(e.target.value);
            setError("");
          }}
          placeholder={t("auth.email")}
          aria-label={t("auth.email")}
          className="w-full rounded-xl border border-neutral-200 bg-white px-4 py-2.5 outline-none focus:border-brand-500 dark:border-neutral-700 dark:bg-neutral-800"
        />
        {error && <p className="text-sm text-red-600">{error}</p>}
        <button type="submit" className={primaryButton}>
          {t("auth.signIn")}
        </button>
        <button
          type="button"
          onClick={() => {
            setDevUser("default");
            onDone();
          }}
          className="text-sm text-neutral-500 underline-offset-4 hover:underline"
        >
          {t("auth.continueDefault")}
        </button>
        <p className="text-xs text-neutral-400">{t("auth.devHint")}</p>
      </form>
    </div>
  );
}

// Signed out with Clerk: the landing page, opening Clerk's sign-up or sign-in window.
function ClerkLanding() {
  const clerk = useClerk();
  return <Landing onStart={() => clerk.openSignUp()} onSignIn={() => clerk.openSignIn()} />;
}

function DevAuth({ children }: { children: ReactNode }) {
  const [signedIn, setSignedIn] = useState(getDevUser() !== null);
  useEffect(() => {
    if (!signedIn) return;
    api.getProfile().catch(() => {
      setDevUser(null); // that user isn't in this database (e.g. switched to the demo store)
      setSignedIn(false);
    });
  }, [signedIn]);
  const [dialog, setDialog] = useState(false);
  if (signedIn) return <>{children}</>;
  return (
    <>
      <Landing onStart={() => setDialog(true)} onSignIn={() => setDialog(true)} />
      {dialog && <DevSignIn onDone={() => setSignedIn(true)} onClose={() => setDialog(false)} />}
    </>
  );
}

export function AuthProvider({ children }: { children: ReactNode }) {
  if (!publishableKey) return <DevAuth>{children}</DevAuth>;
  return (
    <ClerkProvider publishableKey={publishableKey} afterSignOutUrl="/">
      <SignedOut>
        <ClerkLanding />
      </SignedOut>
      <SignedIn>
        <TokenBridge />
        {children}
      </SignedIn>
    </ClerkProvider>
  );
}

// --- Header menu -------------------------------------------------------------------------
function ClerkAccountMenu() {
  const { user } = useUser();
  const { signOut } = useClerk();
  return (
    <AvatarMenu
      name={user?.fullName || user?.primaryEmailAddress?.emailAddress || ""}
      email={user?.primaryEmailAddress?.emailAddress}
      imageUrl={user?.hasImage ? user.imageUrl : undefined}
      onSignOut={() => signOut()}
    />
  );
}

// Dev mode (no login): the default user's name from their profile, and nothing to sign out of.
function DevAccountMenu() {
  const [name, setName] = useState("");
  const [email, setEmail] = useState<string | undefined>();
  useEffect(() => {
    api.getProfile().then((p) => {
      setName(p.name);
      setEmail(p.email || undefined);
    });
  }, []);
  return (
    <AvatarMenu
      name={name}
      email={email}
      onSignOut={() => {
        setDevUser(null);
        window.location.assign(import.meta.env.BASE_URL);
      }}
    />
  );
}

export function AccountMenu() {
  return authEnabled ? <ClerkAccountMenu /> : <DevAccountMenu />;
}

// --- Account card (Subscription page) ----------------------------------------------------
function ClerkAvatar({ name, size }: { name: string; size?: string }) {
  const { user } = useUser();
  return <Avatar name={name} imageUrl={user?.hasImage ? user.imageUrl : undefined} size={size} />;
}

/** The user's photo when signed in with Clerk, else their initials. */
export function AccountAvatar({ name, size }: { name: string; size?: string }) {
  return authEnabled ? <ClerkAvatar name={name} size={size} /> : <Avatar name={name} size={size} />;
}

function ClerkSignOut() {
  const { t } = useI18n();
  const { signOut } = useClerk();
  return (
    <button
      type="button"
      onClick={() => signOut()}
      className="rounded-full px-5 py-2 text-sm font-medium text-red-600 ring-1 ring-red-200 transition hover:bg-red-50 dark:text-red-400 dark:ring-red-900 dark:hover:bg-red-950"
    >
      {t("menu.signOut")}
    </button>
  );
}

/** Sign out (Clerk), or a note that login is off in local development. */
export function SessionControl() {
  const { t } = useI18n();
  return authEnabled ? <ClerkSignOut /> : <p className="text-xs text-neutral-500">{t("auth.devMode")}</p>;
}
