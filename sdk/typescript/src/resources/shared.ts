import { paginate, type PageBody } from "../core/pagination.js";
import type { Query, Transport } from "../core/transport.js";
import type { operations } from "../generated/openapi.js";
import type { RequestOptions } from "../sdk.js";

/** The JSON body type of an operation's response, from the generated wire types. */
export type Json<Op extends keyof operations, S extends keyof operations[Op]["responses"]> = operations[Op]["responses"][S] extends {
  content: { "application/json": infer B };
}
  ? B
  : never;

/** Path segment encoding for ids. */
export const seg = (value: string): string => encodeURIComponent(value);

/** Auto-paging GET over a `{data, has_more, next_cursor}` list operation. */
export function listPages<T>(
  t: Transport,
  path: string,
  query: Query,
  opts: RequestOptions,
  after?: string,
): AsyncIterable<T> {
  return paginate<T>(
    (cursor) => t.json<PageBody<T>>({ method: "GET", path, query: { ...query, after: cursor } }, opts),
    after,
  );
}
