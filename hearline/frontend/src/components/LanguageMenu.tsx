// Website language picker for the header: the current flag, opening a list of flags + names.
import { useCallback, useRef, useState } from "react";

import { LANGUAGES, useI18n } from "../i18n";
import { useDismiss } from "../useDismiss";

export default function LanguageMenu() {
  const { lang, setLang, t } = useI18n();
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const current = LANGUAGES.find((l) => l.code === lang) ?? LANGUAGES[0];

  const close = useCallback(() => setOpen(false), []);
  useDismiss(root, open, close);

  return (
    <div ref={root} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`${t("nav.language")}: ${current.label}`}
        title={t("nav.language")}
        className="flex h-9 items-center gap-1.5 rounded-full px-3 text-sm font-medium text-neutral-500 transition hover:bg-neutral-100 hover:text-neutral-900 dark:hover:bg-neutral-800 dark:hover:text-white"
      >
        <span className="text-lg leading-none">{current.flag}</span>
        <span className="uppercase">{current.code}</span>
        <span className="text-[10px]">▾</span>
      </button>
      {open && (
        <ul
          role="listbox"
          aria-label={t("nav.language")}
          className="absolute right-0 mt-2 w-44 overflow-hidden rounded-xl border border-neutral-200 bg-white py-1 shadow-lg dark:border-neutral-800 dark:bg-neutral-900"
        >
          {LANGUAGES.map((l) => (
            <li key={l.code} role="option" aria-selected={l.code === lang}>
              <button
                type="button"
                onClick={() => {
                  setLang(l.code);
                  setOpen(false);
                }}
                className={`flex w-full items-center gap-3 px-3 py-2 text-left text-sm transition hover:bg-neutral-100 dark:hover:bg-neutral-800 ${
                  l.code === lang ? "font-semibold text-brand-600 dark:text-brand-400" : ""
                }`}
              >
                <span className="text-lg leading-none">{l.flag}</span>
                {l.label}
                {l.code === lang && <span className="ml-auto">✓</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
