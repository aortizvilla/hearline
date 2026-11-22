import { useEffect } from "react";
import { Link, Navigate, Route, Routes } from "react-router-dom";

import { AccountMenu } from "./auth";
import Home from "./pages/Home";
import Settings from "./pages/Settings";
import Subscription from "./pages/Subscription";
import Topic from "./pages/Topic";
import { api } from "./api";
import Logo from "./components/Logo";
import { PLAN_KEY } from "./pages/Landing";

// The plan picked on the landing page before signing up: applied once, if plans are self-serve
function useChosenPlan() {
  useEffect(() => {
    let plan: string | null = null;
    try { plan = localStorage.getItem(PLAN_KEY); localStorage.removeItem(PLAN_KEY); } catch { return; }
    if (!plan || plan === "free") return;
    api.getPlans().then((p) => (p.self_serve ? api.changePlan(plan!) : null)).catch(() => undefined);
  }, []);
}

export default function App() {
  useChosenPlan();
  return (
    <div className="min-h-screen bg-neutral-50 text-neutral-900 antialiased dark:bg-neutral-950 dark:text-neutral-100">
      <header className="sticky top-0 z-10 border-b border-neutral-200/70 bg-neutral-50/85 backdrop-blur dark:border-neutral-800 dark:bg-neutral-950/80">
        <nav className="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
          <Link to="/" aria-label="Hearline">
            <Logo />
          </Link>
          <AccountMenu />
        </nav>
      </header>
      <main className="mx-auto max-w-5xl px-6 py-10">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/topics/:slug" element={<Topic />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/subscription" element={<Subscription />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}
