import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Plans } from "../api";
import { I18nProvider } from "../i18n";
import Landing, { PLAN_KEY } from "./Landing";

const plan = (key: string, label: string, price: number) => ({
  key, label, price_usd_month: price, max_topics: key === "pro" ? null : 2, max_topic_minutes: 5,
  max_episode_minutes: 15, subtopics: key !== "free", voices: "all" as const, tones: "all" as const,
});
let plans: Plans;
vi.mock("../api", async (orig) => ({
  ...(await orig<typeof import("../api")>()),
  api: {
    getOptions: () => Promise.resolve({ tones: [{ value: "calm", label: "Calm" }], languages: [{ value: "en", label: "English" }], voices: [] }),
    getInterests: () => Promise.resolve([{ slug: "film", label: "Film", image: "", suggestions: [{ label: "Film festivals", labels: {} }] }]),
    getPlans: () => Promise.resolve(plans),
  },
}));

const renderLanding = (onStart = vi.fn(), onSignIn = vi.fn()) => {
  render(<I18nProvider><Landing onStart={onStart} onSignIn={onSignIn} /></I18nProvider>);
  return { onStart, onSignIn };
};

describe("Landing page", () => {
  beforeEach(() => {
    localStorage.setItem("uiLanguage", "en");
    plans = { plans: [plan("free", "Free", 0), plan("plus", "Plus", 4.99), plan("pro", "Pro", 9.99)], self_serve: true };
  });

  it("sells the product first, then shows the three steps and the topics", async () => {
    renderLanding();
    expect(screen.getByRole("heading", { level: 1 })).toBeTruthy();
    expect(await screen.findByText("Get in the loop in 3 steps")).toBeTruthy();
    for (const step of ["Pick your topics", "Choose how it sounds", "Listen every morning"]) expect(screen.getByText(step)).toBeTruthy();
    expect(await screen.findByText("Film festivals")).toBeTruthy(); // the first topic's subjects
  });

  it("Start free opens the plans, and continuing remembers the plan and signs up", async () => {
    const { onStart } = renderLanding();
    await screen.findByText("Film festivals"); // data loaded
    fireEvent.click(screen.getAllByRole("button", { name: "Start free" })[0]);
    expect(await screen.findByRole("dialog")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "Choose" })[0]); // Plus
    fireEvent.click(screen.getByRole("button", { name: "Continue with Plus" }));
    expect(onStart).toHaveBeenCalledOnce();
    expect(localStorage.getItem(PLAN_KEY)).toBe("plus");
  });

  it("without billing, only Free can be chosen", async () => {
    plans = { ...plans, self_serve: false };
    renderLanding();
    await screen.findByText("Film festivals");
    fireEvent.click(screen.getAllByRole("button", { name: "Start free" })[0]);
    await screen.findByRole("dialog");
    expect(screen.queryByRole("button", { name: "Choose" })).toBeNull();
    expect(screen.getByText(/Paid plans open soon/)).toBeTruthy();
  });
});
