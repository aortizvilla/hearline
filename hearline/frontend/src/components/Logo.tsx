// The Hearline logo: a line that turns into sound (the mark) and the wordmark in the book serif.
// The small accent dot is the "playing now" colour used across the app.
export function LogoMark({ className = "h-8 w-8" }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden="true">
      <rect width="32" height="32" rx="8" className="fill-brand-600 dark:fill-brand-400" />
      <path d="M5 17h5.5M21.5 17H27" stroke="#fff" strokeWidth="2.4" strokeLinecap="round" />
      <path d="M13 13.5v7M16 9.5v15M19 12v10" stroke="#fff" strokeWidth="2.4" strokeLinecap="round" />
      <circle cx="25.5" cy="9" r="2" className="fill-accent" />
    </svg>
  );
}

export default function Logo({ size = "md" }: { size?: "md" | "lg" }) {
  return (
    <span className="inline-flex items-center gap-2.5" aria-label="Hearline">
      <LogoMark className={size === "lg" ? "h-10 w-10" : "h-8 w-8"} />
      <span className={`font-serif font-semibold tracking-tight ${size === "lg" ? "text-3xl" : "text-2xl"}`} aria-hidden="true">
        Hearline
      </span>
    </span>
  );
}
