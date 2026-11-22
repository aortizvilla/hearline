// Focus subcategories for one topic (e.g. Film > Cannes Festival): removable chips, the
// topic's own suggestions as one-tap "+" chips, and a field to type another. The backend
// classifies each when Settings is saved and returns its canonical name; a phrase that
// doesn't fit the topic is rejected with a message.
import { useState } from "react";

import type { Suggestion } from "../api";
import { useI18n } from "../i18n";

interface Props {
  values: string[]; // canonical English names (what is saved)
  onChange: (values: string[]) => void;
  suggestions?: Suggestion[];
  translations?: Record<string, Record<string, string>>; // saved values' names per language
  max?: number;
}

export default function SubtopicInput({
  values,
  onChange,
  suggestions = [],
  translations = {},
  max = 5,
}: Props) {
  const { t, name } = useI18n();
  // a value is shown in the website language: from the API's saved translations or suggestion
  const labelsOf = (value: string) =>
    translations[value] ?? suggestions.find((s) => s.label === value)?.labels;
  const [draft, setDraft] = useState("");

  const has = (phrase: string) => values.some((v) => v.toLowerCase() === phrase.toLowerCase());
  const addPhrase = (phrase: string) => {
    if (phrase.length >= 2 && !has(phrase) && values.length < max) onChange([...values, phrase]);
  };
  const add = () => {
    addPhrase(draft.trim().slice(0, 60));
    setDraft("");
  };
  const open = suggestions.filter((s) => !has(s.label));

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="mr-1 text-xs text-neutral-500">{t("settings.focus")}</span>
      {values.map((value) => (
        <span
          key={value}
          className="inline-flex items-center gap-1 rounded-full bg-brand-50 py-1 pr-1.5 pl-3 text-xs font-medium text-brand-700 dark:bg-brand-500/15 dark:text-brand-300"
        >
          {name(value, labelsOf(value))}
          <button
            type="button"
            onClick={() => onChange(values.filter((v) => v !== value))}
            aria-label={`${t("settings.focusRemove")} ${name(value, labelsOf(value))}`}
            className="flex h-4 w-4 items-center justify-center rounded-full hover:bg-brand-100 dark:hover:bg-brand-500/30"
          >
            ×
          </button>
        </span>
      ))}
      {values.length < max &&
        open.map((suggestion) => (
          <button
            key={suggestion.label}
            type="button"
            onClick={() => addPhrase(suggestion.label)}
            className="rounded-full border border-dashed border-neutral-300 px-3 py-1 text-xs text-neutral-600 transition hover:border-brand-500 hover:text-brand-600 dark:border-neutral-600 dark:text-neutral-300"
          >
            + {name(suggestion.label, suggestion.labels)}
          </button>
        ))}
      {values.length < max && (
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add();
            }
          }}
          onBlur={add}
          maxLength={60}
          placeholder={t("settings.focusPlaceholder")}
          className="min-w-48 flex-1 rounded-full border border-neutral-200 bg-white px-3 py-1 text-xs outline-none focus:border-brand-500 dark:border-neutral-700 dark:bg-neutral-800"
        />
      )}
    </div>
  );
}
