// The unmodified `openai` npm client against the stack's facade (tests/e2e/suite/test_facade_sdks.py runs this).
// baseURL = NOEMA_E2E_FACADE (<api>/openai/v1 on Noema; never OpenAI). Every response is recorded in `seen`
// (status, headers, raw body, the request JSON) so that the Python side validates it against the contract.
import OpenAI from "openai";

const env = process.env;
// Noema's local facade only: without this guard a missing variable makes the openai client fall back to
// OpenAI's public endpoint with a Noema key (node subprocesses are not covered by pytest-socket).
const LOCAL = /^http:\/\/(127\.0\.0\.1|localhost)(:[0-9]+)?\//;
if (!LOCAL.test(env.NOEMA_E2E_FACADE ?? "")) {
  throw new Error(`NOEMA_E2E_FACADE must be a local http://127.0.0.1 or http://localhost URL, got ${env.NOEMA_E2E_FACADE}`);
}
const seen = [];
async function recording(input, init) {
  const url = typeof input === "string" ? input : String(input.url ?? input);
  if (!url.startsWith(env.NOEMA_E2E_FACADE)) throw new Error(`refusing a request outside the facade: ${url}`);
  const response = await fetch(input, init);
  seen.push({
    url,
    status: response.status,
    headers: Object.fromEntries(response.headers.entries()),
    body: await response.clone().text(),
    request: typeof init?.body === "string" ? init.body : null,
  });
  return response;
}

const client = new OpenAI({ apiKey: env.NOEMA_E2E_KEY, baseURL: env.NOEMA_E2E_FACADE, maxRetries: 0, fetch: recording });
const out = {};
out.plain = await client.decisions.create(JSON.parse(env.NOEMA_E2E_BODY));
out.ensemble = await client.decisions.create(JSON.parse(env.NOEMA_E2E_BODY2), {
  headers: { "x-aghoo-include": "ensemble" },
});
try {
  await client.decisions.create({ model: "router-default", input: "x", questions: [{ type: "predicate", instructions: "Is it?" }] });
  out.error = null;
} catch (error) {
  out.error = { name: error.constructor.name, status: error.status, code: error.code };
}
process.stdout.write(JSON.stringify({ out, seen }));
