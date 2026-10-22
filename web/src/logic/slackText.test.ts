import { describe, expect, test } from "vitest";
import { renameMention } from "./slackText";

const POST = [
  "- [One](https://github.com/o/r/pull/1) @alan `+1/-1`",
  "  - [Two](https://github.com/o/r/pull/12) @alan `+2/-2`",
  "- [Three](https://github.com/o/r/pull/123) @alan `+3/-3`",
].join("\n");

describe("renameMention", () => {
  test("renames the name on every line", () => {
    expect(renameMention(POST, "alan", "will")).toBe(POST.split("@alan").join("@will"));
  });

  test("renames it only on the lines of the PR given, whose number may start another's", () => {
    const renamed = renameMention(POST, "alan", "will", 12).split("\n");

    expect(renamed[0]).toContain("@alan");
    expect(renamed[1]).toContain("@will");
    expect(renamed[2]).toContain("@alan"); // pull/123 is not pull/12
  });

  test("does not rename a longer name that starts the same", () => {
    expect(renameMention("@alan.andradecestelos and @alan", "alan", "will")).toBe(
      "@alan.andradecestelos and @will",
    );
  });

  test("renames a name that ends a sentence", () => {
    expect(renameMention("Please look today @alan.", "alan", "will")).toBe("Please look today @will.");
  });

  test("names with dots are matched literally", () => {
    expect(renameMention("@daniel.baptista.dias @danielXbaptista", "daniel.baptista.dias", "will")).toBe(
      "@will @danielXbaptista",
    );
  });
});
