import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { createAppStore } from "../store/appStore";
import { StoreProvider } from "../store/StoreContext";
import { RefreshButton } from "./RefreshButton";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function show(fetchImpl: typeof fetch) {
  vi.stubGlobal("fetch", fetchImpl);
  const store = createAppStore();
  render(
    <StoreProvider store={store}>
      <RefreshButton id="1:devon/a" />
    </StoreProvider>,
  );
  return store;
}

const accepted = vi.fn(async () => ({ ok: true, status: 202, json: async () => ({ branches: 3 }) }));

describe("the refresh button", () => {
  test("asks the server to look this PR up again", async () => {
    show(accepted as unknown as typeof fetch);

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(accepted).toHaveBeenCalled());
    expect(accepted.mock.calls[0]).toEqual([
      "/api/refresh",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ id: "1:devon/a" }) }),
    ]);
  });

  test("says it is refreshing, disabled, until the card's lookup arrives", async () => {
    const store = show(accepted as unknown as typeof fetch);

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    expect(await screen.findByRole("button", { name: "Refreshing…" })).toBeDisabled();
    act(() => store.getState().refreshed(1, [{ branch_id: "1:devon/a", fetched_at: "2026-10-23T20:00:00+00:00", error: null }]));
    expect(await screen.findByRole("button", { name: "Refresh" })).toBeEnabled();
  });

  test("another card's lookup does not end it", async () => {
    const store = show(accepted as unknown as typeof fetch);

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await screen.findByRole("button", { name: "Refreshing…" });
    act(() =>
      store.getState().refreshed(1, [{ branch_id: "1:devon/other", fetched_at: "2026-10-23T20:00:00+00:00", error: null }]),
    );

    expect(screen.getByRole("button", { name: "Refreshing…" })).toBeDisabled();
  });

  test("stops saying it is refreshing if nothing arrives", async () => {
    vi.useFakeTimers();
    show(accepted as unknown as typeof fetch);

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(screen.getByRole("button", { name: "Refreshing…" })).toBeDisabled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(31_000);
    });

    expect(screen.getByRole("button", { name: "Refresh" })).toBeEnabled();
  });

  test("a refusal says why and lets it be pressed again", async () => {
    show((async () => ({ ok: false, status: 500, json: async () => ({}) })) as unknown as typeof fetch);

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("/api/refresh: HTTP 500");
    expect(screen.getByRole("button", { name: "Refresh" })).toBeEnabled();
  });
});
