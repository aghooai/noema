/**
 * Live checks against a running Noema API (no model calls). Skipped unless NOEMA_LIVE=1 with NOEMA_LIVE_BASE_URL and
 * NOEMA_LIVE_API_KEY set.
 */
import { describe, expect, it } from "vitest";
import { AghooClient, AghooError } from "../src/index";

const enabled = process.env.NOEMA_LIVE === "1" && !!process.env.NOEMA_LIVE_BASE_URL && !!process.env.NOEMA_LIVE_API_KEY;

describe.skipIf(!enabled)("live API (NOEMA_LIVE=1)", () => {
  it("profiles, a session round trip and usage", async () => {
    const c = new AghooClient({ baseURL: process.env.NOEMA_LIVE_BASE_URL!, apiKey: process.env.NOEMA_LIVE_API_KEY! });
    for (const p of await c.profiles.list()) expect(p.version).toMatch(/^[0-9]+$/);
    const s = await c.sessions.create({ metadata: { sdk: "typescript-live" } });
    expect((await c.sessions.retrieve(s.id)).id).toBe(s.id);
    expect((await c.sessions.update(s.id, { metadata: {} })).metadata).toEqual({});
    expect((await c.sessions.delete(s.id)).deleted).toBe(true);
    for await (const _ of c.usage.list({ limit: 1 })) break;
    await expect(c.sessions.retrieve("ses_does_not_exist")).rejects.toBeInstanceOf(AghooError);
  });
});
