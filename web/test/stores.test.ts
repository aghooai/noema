import type { UsageRow } from "@aghoo/sdk";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { readCookie } from "../src/api/client";
import { CalibrationStore } from "../src/calibration/calibrationStore";
import { UsageStore } from "../src/usage/usageStore";
import { BASE, CSRF, fixture, respond, testApi, useServer } from "./helpers";

const server = useServer();

const report = (groupBy: string, data: UsageRow[]) => ({ group_by: groupBy, from: "2026-09-11", to: "2026-10-10", data, has_more: false, next_cursor: null });

describe("UsageStore (W7)", () => {
  it("passes group_by/from/to and follows next_cursor (getUsage fixture pages)", async () => {
    const queries: string[] = [];
    server.use(
      http.get(`${BASE}/v1/usage`, ({ request }) => {
        const q = new URL(request.url).searchParams;
        queries.push(q.toString());
        return respond(fixture(q.has("after") ? "getUsage.page2" : "getUsage.page1"));
      }),
    );
    const store = new UsageStore(testApi().client);
    await store.load({ groupBy: "model", from: "2026-09-11", to: "2026-10-10" });
    const all = [...(fixture("getUsage.page1").body as { data: UsageRow[] }).data, ...(fixture("getUsage.page2").body as { data: UsageRow[] }).data];
    expect(store.state.rows).toEqual(all);
    expect(queries[0]).toBe("limit=200&group_by=model&from=2026-09-11&to=2026-10-10");
    expect(queries).toHaveLength(2);
  });

  it("a slower earlier load never overwrites a newer grouping", async () => {
    let releaseModel: () => void = () => undefined;
    const modelGate = new Promise<void>((r) => {
      releaseModel = r;
    });
    const row = (key: string): UsageRow => ({ key, line: "generation", usage: (fixture("getUsage.page1").body as { data: UsageRow[] }).data[0]!.usage });
    server.use(
      http.get(`${BASE}/v1/usage`, async ({ request }) => {
        const g = new URL(request.url).searchParams.get("group_by") as string;
        if (g === "model") await modelGate;
        return HttpResponse.json(report(g, [row(g === "model" ? "us.openai.gpt-6.1-sol" : "ses_1")]));
      }),
    );
    const store = new UsageStore(testApi().client);
    const first = store.load({ groupBy: "model" });
    await store.load({ groupBy: "session" });
    releaseModel();
    await first;
    expect(store.state.groupBy).toBe("session");
    expect(store.state.rows.map((r) => r.key)).toEqual(["ses_1"]);
    expect(store.state.loading).toBe(false);
  });

  it("drills from a session to its assistant turns and into one turn", async () => {
    server.use(
      http.get(`${BASE}/v1/sessions/:id/turns`, () => respond(fixture("listSessionTurns.page2"))),
      http.get(`${BASE}/v1/turns/:id`, () => respond(fixture("getTurn.200"))),
    );
    const store = new UsageStore(testApi().client);
    await store.openSession("ses_1");
    expect(store.state.sessionTurns.every((t) => t.role === "assistant")).toBe(true);
    await store.openTurn("turn_1");
    expect(store.state.turn?.usage?.cost_usd).toBe(0.0123);
  });
});

describe("CalibrationStore (W4)", () => {
  it("loads the three lists; a 403 on one list (listCalibrators.403 fixture) leaves the others intact", async () => {
    const seen: string[] = [];
    server.use(
      http.get(`${BASE}/v1/admin/calibrators`, ({ request }) => {
        seen.push(new URL(request.url).search);
        return respond(fixture("listCalibrators.200"));
      }),
      http.get(`${BASE}/v1/admin/labeling/runs`, () => respond(fixture("listLabelingRuns.200"))),
      http.get(`${BASE}/v1/admin/labeling/dropped`, () => respond(fixture("listCalibrators.403"))),
    );
    const store = new CalibrationStore(testApi().client);
    await store.filter({ status: "active" });
    expect(seen).toEqual(["?limit=100&status=active"]);
    expect(store.state.calibrators.rows.length).toBeGreaterThan(0);
    expect(store.state.runs.rows.length).toBeGreaterThan(0);
    expect(store.state.dropped.error).toContain("permission_denied");
  });
});

describe("SPA API wiring (spec-09 §2 dev login)", () => {
  it("dev-login calls carry the CSRF header on POST only; no Authorization header is ever sent", async () => {
    const seen: [string, string | null, string | null][] = [];
    const state = { authenticated: true, alias: "developer", aliases: ["developer"] };
    const record = ({ request }: { request: Request }) => {
      seen.push([`${request.method} ${new URL(request.url).pathname}`, request.headers.get("x-csrf-token"), request.headers.get("authorization")]);
      return HttpResponse.json(state);
    };
    server.use(http.get(`${BASE}/auth/session`, record), http.post(`${BASE}/auth/login`, record), http.post(`${BASE}/auth/logout`, record));
    const api = testApi();
    expect(await api.auth.session()).toEqual(state);
    await api.auth.login("developer");
    await api.auth.logout();
    expect(seen).toEqual([
      ["GET /auth/session", null, null],
      ["POST /auth/login", CSRF, null],
      ["POST /auth/logout", CSRF, null],
    ]);
  });

  it("readCookie finds one cookie in a document.cookie string", () => {
    expect(readCookie("noema_csrf", "a=1; noema_csrf=abc%3D; b=2")).toBe("abc=");
    expect(readCookie("noema_csrf", "noema_csrf_x=1")).toBeNull();
  });
});
