import { execFileSync } from "node:child_process";
import { rmSync } from "node:fs";
import { join, resolve } from "node:path";
import { REPO } from "./fixture-data.ts";

export const MEDIA_DIR = resolve(REPO, "web/test-results/media");

/** Fresh recording for the contract check, and the generated upload files (needs the project venv: pikepdf, Pillow). */
export default function globalSetup(): void {
  rmSync(resolve(REPO, "web", process.env.NOEMA_UI_RECORD ?? "test-results/api-recording.jsonl"), { force: true });
  execFileSync("uv", ["run", "--frozen", "--no-sync", "python", join(REPO, "web/e2e/make_media.py"), MEDIA_DIR], {
    cwd: REPO,
    env: { ...process.env, UV_NO_SYNC: "1", PYTHONPATH: REPO },
    stdio: "inherit",
  });
}
