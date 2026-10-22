import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { createAppStore } from "../store/appStore";
import { StoreProvider } from "../store/StoreContext";
import { UpdateNotice } from "./UpdateNotice";

afterEach(() => {
  document.head.innerHTML = "";
  vi.unstubAllGlobals();
});

const pageRunning = (file: string): void => {
  const element = document.createElement("script");
  element.type = "module";
  element.src = `/assets/${file}`;
  document.head.appendChild(element);
};

function show() {
  const store = createAppStore();
  render(
    <StoreProvider store={store}>
      <UpdateNotice />
    </StoreProvider>,
  );
  return store;
}

describe("the update notice", () => {
  test("says nothing while the server serves the build this page runs", () => {
    pageRunning("index-old.js");
    const store = show();

    act(() => store.getState().hello(1, 0, "index-old.js"));

    expect(screen.queryByRole("status")).toBeNull();
  });

  test("offers a reload once the server serves another build", () => {
    pageRunning("index-old.js");
    const store = show();

    act(() => store.getState().hello(1, 0, "index-new.js"));

    expect(screen.getByRole("status")).toHaveTextContent("tt ui was updated.");
    const reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload });
    act(() => screen.getByRole("button", { name: "Reload" }).click());
    expect(reload).toHaveBeenCalledOnce();
  });

  test("stays once shown: a later hello of the same build does not hide it", () => {
    pageRunning("index-old.js");
    const store = show();
    act(() => store.getState().hello(1, 0, "index-new.js"));

    act(() => store.getState().hello(2, 0, "index-new.js"));

    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  test("never calls a page out of date when it cannot tell its own build or the server's", () => {
    const store = show(); // no built script in the document

    act(() => store.getState().hello(1, 0, "index-new.js"));
    expect(screen.queryByRole("status")).toBeNull();

    pageRunning("index-old.js");
    act(() => store.getState().hello(2, 0, null)); // the server has no build
    expect(screen.queryByRole("status")).toBeNull();
  });
});
