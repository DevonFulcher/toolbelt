import { afterEach, describe, expect, test } from "vitest";
import { runningBuild } from "./build";

afterEach(() => {
  document.head.innerHTML = "";
});

const script = (src: string): void => {
  const element = document.createElement("script");
  element.type = "module";
  element.src = src;
  document.head.appendChild(element);
};

describe("runningBuild", () => {
  test("is the file name of the page's main script", () => {
    script("/assets/index-C2k9uj7O.js");

    expect(runningBuild()).toBe("index-C2k9uj7O.js");
  });

  test("cannot be told without a built script (the dev server, tests)", () => {
    expect(runningBuild()).toBeNull();
    script("/src/main.tsx");

    expect(runningBuild()).toBeNull();
  });
});
