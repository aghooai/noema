// Gate Q2 with the unmodified `openai` npm client against the stack's facade (tests/qa/contract/test_facade_typescript.py
// runs this). baseURL = NOEMA_QA_FACADE (<api>/openai/v1 on Noema; never OpenAI). NOEMA_QA_CASES is a JSON list of
// {name, body, headers?, maxRetries?, key?}; each case runs client.decisions.create(body, {headers, maxRetries}) and
// its outcome is reported as {ok: response} or {error: {name, status, code}}. Every HTTP response the client saw is
// recorded in `seen` (case, status, headers, raw body, request JSON) so that the Python side validates it against
// the contract.
import OpenAI from "openai";

const env = process.env;
// Noema's local facade only: without this guard a missing variable makes the openai client fall back to
// OpenAI's public endpoint (node subprocesses are not covered by pytest-socket).
const LOCAL = /^http:\/\/(127\.0\.0\.1|localhost)(:[0-9]+)?\//;
if (!LOCAL.test(env.NOEMA_QA_FACADE ?? "")) {
  throw new Error(`NOEMA_QA_FACADE must be a local http://127.0.0.1 or http://localhost URL, got ${env.NOEMA_QA_FACADE}`);
}
const seen = [];
let current = null;
async function recording(input, init) {
  const url = typeof input === "string" ? input : String(input.url ?? input);
  if (!url.startsWith(env.NOEMA_QA_FACADE)) throw new Error(`refusing a request outside the facade: ${url}`);
  const response = await fetch(input, init);
  seen.push({
    case: current,
    url,
    status: response.status,
    headers: Object.fromEntries(response.headers.entries()),
    body: await response.clone().text(),
    request: typeof init?.body === "string" ? init.body : null,
  });
  return response;
}

const results = {};
for (const c of JSON.parse(env.NOEMA_QA_CASES)) {
  current = c.name;
  const client = new OpenAI({ apiKey: c.key ?? env.NOEMA_QA_KEY, baseURL: env.NOEMA_QA_FACADE, fetch: recording });
  try {
    const ok = await client.decisions.create(c.body, { headers: c.headers ?? {}, maxRetries: c.maxRetries ?? 0 });
    results[c.name] = { ok };
  } catch (error) {
    results[c.name] = { error: { name: error.constructor.name, status: error.status ?? null, code: error.code ?? null } };
  }
}
process.stdout.write(JSON.stringify({ results, seen }));
