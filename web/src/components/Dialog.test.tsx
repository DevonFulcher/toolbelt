import { render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { Dialog } from "./Dialog";

test("a render with a new onClose leaves focus where the user put it", () => {
  const tree = (onClose: () => void) => (
    <Dialog title="t" onClose={onClose}>
      <select aria-label="pick">
        <option>a</option>
      </select>
    </Dialog>
  );
  const { rerender } = render(tree(() => {}));
  const select = screen.getByLabelText("pick");
  select.focus();

  rerender(tree(() => {}));

  expect(document.activeElement).toBe(select);
});

test("Escape calls the latest onClose", () => {
  const first = vi.fn();
  const second = vi.fn();
  const { rerender } = render(<Dialog title="t" onClose={first}>x</Dialog>);
  rerender(<Dialog title="t" onClose={second}>x</Dialog>);

  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));

  expect(first).not.toHaveBeenCalled();
  expect(second).toHaveBeenCalledTimes(1);
});
