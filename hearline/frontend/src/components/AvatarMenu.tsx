// The account menu in the header: the user's avatar, opening Preferences, Subscription, the
// website language, light/dark mode and Sign out. auth.tsx supplies the name, photo and
// sign-out (Clerk, or the dev user).
import { useCallback, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { LANGUAGES, useI18n } from "../i18n";
import { useTheme } from "../theme";
import { useDismiss } from "../useDismiss";

interface Props {
  name: string;
  email?: string;
  imageUrl?: string;
  onSignOut?: () => void; // omitted when there is no login (dev mode)
}

export const initials = (name: string): string =>
  name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0].toUpperCase())
    .join("") || "?";

export function Avatar({ name, imageUrl, size = "h-9 w-9 text-sm" }: { name: string; imageUrl?: string; size?: string }) {
  return imageUrl ? (
    <img src={imageUrl} alt="" referrerPolicy="no-referrer" className={`${size} rounded-full object-cover`} />
  ) : (
    <span
      className={`${size} flex items-center justify-center rounded-full bg-gradient-to-br from-brand-500 to-brand-700 font-semibold text-white`}
    >
      {initials(name)}
    </span>
  );
}

const item =
  "flex w-full items-center gap-3 px-4 py-2 text-left text-sm transition hover:bg-neutral-100 dark:hover:bg-neutral-800";

export default function AvatarMenu({ name, email, imageUrl, onSignOut }: Props) {
  const { lang, setLang, t } = useI18n();
  const [theme, toggleTheme] = useTheme();
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(root, open, close);

  return (
    <div ref={root} className="relative ml-1">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t("menu.account")}
        className="rounded-full ring-offset-2 transition hover:ring-2 hover:ring-brand-500 focus-visible:ring-2 focus-visible:ring-brand-500 dark:ring-offset-neutral-950"
      >
        <Avatar name={name} imageUrl={imageUrl} />
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 mt-2 w-60 overflow-hidden rounded-xl border border-neutral-200 bg-white py-1 shadow-lg dark:border-neutral-800 dark:bg-neutral-900"
        >
          <div className="flex items-center gap-3 border-b border-neutral-200 px-4 py-3 dark:border-neutral-800">
            <Avatar name={name} imageUrl={imageUrl} size="h-10 w-10 text-sm" />
            <div className="min-w-0">
              <p className="truncate text-sm font-medium">{name}</p>
              {email && <p className="truncate text-xs text-neutral-500">{email}</p>}
            </div>
          </div>
          <Link to="/settings" role="menuitem" onClick={close} className={item}>
            <span aria-hidden>⚙</span>
            {t("menu.preferences")}
          </Link>
          <Link to="/subscription" role="menuitem" onClick={close} className={item}>
            <span aria-hidden>★</span>
            {t("menu.subscription")}
          </Link>
          <div className="border-t border-neutral-200 px-4 pt-3 pb-2 dark:border-neutral-800">
            <p className="mb-2 text-xs text-neutral-500">{t("nav.language")}</p>
            <div className="grid grid-cols-6 gap-1" role="group" aria-label={t("nav.language")}>
              {LANGUAGES.map((l) => (
                <button
                  key={l.code}
                  type="button"
                  onClick={() => setLang(l.code)}
                  aria-pressed={l.code === lang}
                  aria-label={l.label}
                  title={l.label}
                  className={`flex h-8 items-center justify-center rounded-lg text-lg transition ${
                    l.code === lang
                      ? "bg-brand-50 ring-2 ring-brand-500 dark:bg-brand-500/15"
                      : "hover:bg-neutral-100 dark:hover:bg-neutral-800"
                  }`}
                >
                  {l.flag}
                </button>
              ))}
            </div>
          </div>
          <button
            type="button"
            role="menuitem"
            onClick={toggleTheme}
            aria-label={t(theme === "dark" ? "theme.toLight" : "theme.toDark")}
            className={item}
          >
            <span aria-hidden>{theme === "dark" ? "☀" : "☾"}</span>
            {t(theme === "dark" ? "theme.light" : "theme.dark")}
          </button>
          {onSignOut && (
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                close();
                onSignOut();
              }}
              className={`${item} border-t border-neutral-200 text-red-600 dark:border-neutral-800 dark:text-red-400`}
            >
              <span aria-hidden>⎋</span>
              {t("menu.signOut")}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
