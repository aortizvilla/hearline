// Light / dark switch for pages without the account menu (the landing page). Same setting as
// the account menu's (theme.ts): it starts from the browser's preference and is remembered.
import { useI18n } from "../i18n";
import { useTheme } from "../theme";

export default function ThemeToggle() {
  const { t } = useI18n();
  const [theme, toggleTheme] = useTheme();
  const label = t(theme === "dark" ? "theme.toLight" : "theme.toDark");
  return (
    <button
      type="button"
      onClick={toggleTheme}
      aria-label={label}
      title={label}
      className="flex h-9 w-9 items-center justify-center rounded-full text-lg text-neutral-500 transition hover:bg-black/5 hover:text-neutral-900 focus-visible:outline-2 focus-visible:outline-brand-500 dark:hover:bg-white/10 dark:hover:text-white"
    >
      <span aria-hidden>{theme === "dark" ? "☀" : "☾"}</span>
    </button>
  );
}
