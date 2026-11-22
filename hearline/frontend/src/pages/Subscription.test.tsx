import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "../i18n";
import Subscription from "./Subscription";

const deleteAccount = vi.fn(() => Promise.resolve({ deleted: true }));
vi.mock("../api", async (orig) => ({
  ...(await orig<typeof import("../api")>()),
  api: {
    getProfile: () => Promise.resolve({ name: "Ana", email: "ana@x.com", tone: "calm", language: "en", voice: "aria", topics: [], plan: "free" }),
    getPlans: () => Promise.resolve({ plans: [], self_serve: true }),
    deleteAccount: () => deleteAccount(),
  },
}));

describe("Subscription page: deleting the account", () => {
  beforeEach(() => {
    localStorage.setItem("uiLanguage", "en");
    deleteAccount.mockClear();
    Object.defineProperty(window, "location", { value: { ...window.location, assign: vi.fn() }, writable: true });
  });

  it("asks for confirmation before erasing anything", async () => {
    render(<I18nProvider><Subscription /></I18nProvider>);
    fireEvent.click(await screen.findByRole("button", { name: "Delete account" }));
    expect(deleteAccount).not.toHaveBeenCalled();
    expect(screen.getByText("Delete everything for good?")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Delete account" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Delete account" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete permanently" }));
    await vi.waitFor(() => expect(deleteAccount).toHaveBeenCalledOnce());
    await vi.waitFor(() => expect(window.location.assign).toHaveBeenCalledWith("/"));
  });
});
