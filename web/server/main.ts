import { appendFileSync, existsSync } from "node:fs";
import { createServer } from "node:http";
import { join } from "node:path";
import { createHandler, type Recorded } from "./app.ts";
import { ConfigError, configFromEnv } from "./config.ts";

/** `node server/main.ts`: the dev cookie proxy (spec-09 §2). Refuses to start unless NOEMA_ENV=dev. */
function main(): void {
  let config;
  try {
    config = configFromEnv(process.env);
  } catch (e) {
    console.error(`noema-web: ${e instanceof ConfigError ? e.message : String(e)}`);
    process.exit(2);
  }
  if (!existsSync(join(config.distDir, "index.html"))) {
    console.error(`noema-web: ${config.distDir}/index.html is missing; run \`npm run build\` (make ui-build) first`);
    process.exit(2);
  }
  if (config.keys.size === 0) console.warn("noema-web: no API keys configured (NOEMA_UI_API_KEYS / NOEMA_UI_API_KEYS_FILE)");
  const file = config.recordFile;
  const record = file === null ? null : (r: Recorded): void => appendFileSync(file, `${JSON.stringify(r)}\n`, { mode: 0o600 });
  const server = createServer(createHandler(config, undefined, record));
  server.listen(config.port, config.host, () => {
    const addr = server.address();
    const port = typeof addr === "object" && addr !== null ? addr.port : config.port;
    console.log(`noema-web: http://${config.host}:${port} -> ${config.apiUrl} (aliases: ${[...config.keys.keys()].join(", ") || "none"})`);
  });
  const stop = (): void => {
    server.close();
    server.closeAllConnections();
  };
  process.on("SIGTERM", stop);
  process.on("SIGINT", stop);
}

main();
