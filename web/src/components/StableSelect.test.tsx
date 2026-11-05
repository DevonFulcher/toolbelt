import { render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { StableSelect } from "./StableSelect";

const OPTIONS = [
  { value: "", label: "Any" },
  { value: "a", label: "A" },
];

afterEach(() => vi.restoreAllMocks());

test("a render with the same value and options leaves the DOM select alone", () => {
  const { rerender } = render(<StableSelect value="a" options={OPTIONS} onChange={() => {}} />);
  const writes = vi.spyOn(HTMLOptionElement.prototype, "selected", "set");

  rerender(<StableSelect value="a" options={OPTIONS.map((o) => ({ ...o }))} onChange={() => {}} />);

  expect(writes).not.toHaveBeenCalled();
});

test("a new value or option list is shown, and the latest handler is called", () => {
  const first = vi.fn();
  const second = vi.fn();
  const { rerender, container } = render(<StableSelect value="" options={OPTIONS} onChange={first} />);
  rerender(
    <StableSelect value="b" options={[...OPTIONS, { value: "b", label: "B" }]} onChange={second} />,
  );
  const select = container.querySelector("select")!;

  expect(select.value).toBe("b");
  select.value = "a";
  select.dispatchEvent(new Event("change", { bubbles: true }));
  expect(second).toHaveBeenCalledWith("a");
  expect(first).not.toHaveBeenCalled();
});
