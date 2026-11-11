import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { CopyButton } from "./CopyButton";

afterEach(() => vi.unstubAllGlobals());

test("copies the text without activating what the button sits in, and says it copied", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  const outer = vi.fn();
  render(
    <a href="#x" onClick={outer}>
      <CopyButton text="devon/a" what="branch name" />
    </a>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Copy branch name" }));
  expect(writeText).toHaveBeenCalledWith("devon/a");
  expect(outer).not.toHaveBeenCalled();
  await waitFor(() => expect(screen.getByRole("button", { name: "Copied" })).toBeTruthy());
});

test("says so when the clipboard refuses", async () => {
  vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
  render(<CopyButton text="1" what="PR number" />);
  fireEvent.click(screen.getByRole("button", { name: "Copy PR number" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Copy failed" })).toBeTruthy());
});
