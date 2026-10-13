import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import type { IncomingMessage, ServerResponse } from "node:http";
import { extname, join, normalize, sep } from "node:path";

const TYPES: Readonly<Record<string, string>> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".txt": "text/plain; charset=utf-8",
};

/**
 * Serves the built SPA from `root`: "/" is index.html, everything else must be a file under root with a known type
 * (no directory listing, no traversal, no SVG). Hash routing means there is no SPA fallback.
 */
export async function serveStatic(root: string, req: IncomingMessage, res: ServerResponse, pathname: string): Promise<void> {
  if (req.method !== "GET" && req.method !== "HEAD") {
    res.writeHead(405, { allow: "GET, HEAD" }).end();
    return;
  }
  let rel: string;
  try {
    rel = pathname === "/" ? "index.html" : decodeURIComponent(pathname).replace(/^\/+/, "");
  } catch {
    rel = "\0";
  }
  const file = normalize(join(root, rel));
  const type = TYPES[extname(file)];
  if (!file.startsWith(root.endsWith(sep) ? root : root + sep) || type === undefined || rel.includes("\0")) {
    res.writeHead(404, { "content-type": "text/plain; charset=utf-8" }).end("not found");
    return;
  }
  try {
    const st = await stat(file);
    if (!st.isFile()) throw new Error("not a file");
    const immutable = rel.startsWith("assets/");
    res.writeHead(200, {
      "content-type": type,
      "content-length": String(st.size),
      "cache-control": immutable ? "public, max-age=31536000, immutable" : "no-store",
    });
    if (req.method === "HEAD") res.end();
    else createReadStream(file).pipe(res);
  } catch {
    res.writeHead(404, { "content-type": "text/plain; charset=utf-8" }).end("not found");
  }
}
