import type { Transport } from "../core/transport.js";
import type { ProfileInfo, RequestOptions } from "../sdk.js";
import type { Json } from "./shared.js";

/** Decision and generation profiles the caller may select. */
export class Profiles {
  readonly #t: Transport;
  constructor(t: Transport) {
    this.#t = t;
  }

  /** Profiles after the tenant allowlist (Phase 1 omits `best_of_n` / `fuse` generation profiles). */
  async list(params: { kind?: "decision" | "generation" } = {}, opts: RequestOptions = {}): Promise<ProfileInfo[]> {
    const spec = { method: "GET", path: "/v1/profiles", query: { kind: params.kind } } as const;
    return (await this.#t.json<Json<"listProfiles", 200>>(spec, opts)).data;
  }
}
