import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom lays nothing out; its scrollTo only logs "not implemented".
window.scrollTo = () => {};

afterEach(() => {
  cleanup();
});
