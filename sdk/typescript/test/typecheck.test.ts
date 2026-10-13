import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SDK = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const tsc = join(dirname(createRequire(import.meta.url).resolve("typescript/package.json")), "bin/tsc");

describe("type-level contract checks (tsc --strict)", () => {
  it("wire-compat.check.ts, sdk-usage.check.ts and implements.check.ts compile with every @ts-expect-error used", () => {
    const run = () => execFileSync(process.execPath, [tsc, "-p", join(SDK, "tsconfig.json")], { encoding: "utf8" });
    expect(run).not.toThrow();
  }, 120_000);
});
