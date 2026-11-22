// The admin dashboard: its own app at admin.<domain> (admin.localhost:5173 locally), separate
// from the product. Same login (Clerk) and API; the backend only answers admins
// (ADMIN_USER_IDS), so the dashboard's code holds no data on its own.
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { AuthProvider, SessionControl } from "../auth";
import LanguageMenu from "../components/LanguageMenu";
import { I18nProvider } from "../i18n";
import "../index.css";
import Dashboard from "./Dashboard";
import Logo from "../components/Logo";

function AdminApp() {
  return (
    <div className="min-h-screen bg-neutral-50 text-neutral-900 antialiased dark:bg-neutral-950 dark:text-neutral-100">
      <header className="sticky top-0 z-10 border-b border-neutral-200/70 bg-neutral-50/85 backdrop-blur dark:border-neutral-800 dark:bg-neutral-950/80">
        <nav className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-6 py-4">
          <span className="flex items-center gap-2">
            <Logo />{" "}
            <span className="font-normal text-neutral-500">Admin</span>
          </span>
          <div className="flex items-center gap-3">
            <LanguageMenu />
            <SessionControl />
          </div>
        </nav>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-10">
        <Dashboard />
      </main>
    </div>
  );
}

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <I18nProvider>
      <AuthProvider>
        <BrowserRouter basename={import.meta.env.BASE_URL}>
          <AdminApp />
        </BrowserRouter>
      </AuthProvider>
    </I18nProvider>
  </StrictMode>,
);
