import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { REPO } from "./helpers";

const SDK = resolve(dirname(fileURLToPath(import.meta.url)), "..");

describe("vendored contract surface", () => {
  it("types.ts, sdk.ts, openai-compat.ts, sdk-usage.check.ts and the generated wire types are in sync", () => {
    const out = execFileSync(process.execPath, [join(SDK, "scripts/sync-contracts.mjs"), "--check"], { encoding: "utf8" });
    expect(out.trim()).toBe("contracts in sync");
  });

  it("types.ts and sdk.ts are byte copies of the vendored contracts", () => {
    for (const name of ["types.ts", "sdk.ts"]) {
      const vendored = readFileSync(join(REPO, "src/noema/contracts/_vendor", name));
      expect(readFileSync(join(SDK, "src", name)).equals(vendored)).toBe(true);
    }
  });
});
