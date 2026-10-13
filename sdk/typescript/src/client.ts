import { type ClientOptions, Transport } from "./core/transport.js";
import { Decisions } from "./resources/decisions.js";
import { Files } from "./resources/files.js";
import { Profiles } from "./resources/profiles.js";
import { Sessions } from "./resources/sessions.js";
import { Turns } from "./resources/turns.js";
import { Admin, Usage } from "./resources/usage.js";

/**
 * Client for the Noema API. Every model call behind it runs on Amazon Bedrock. Non-2xx responses throw AghooError
 * after retries (connect errors, timeouts and retryable codes; full jitter, `retry-after` honored; POSTs carry one
 * Idempotency-Key across their retries).
 */
export class AghooClient {
  readonly decisions: Decisions;
  readonly turns: Turns;
  readonly profiles: Profiles;
  readonly sessions: Sessions;
  readonly files: Files;
  readonly usage: Usage;
  readonly admin: Admin;

  constructor(opts: ClientOptions = {}) {
    const t = new Transport(opts);
    this.decisions = new Decisions(t);
    this.turns = new Turns(t);
    this.profiles = new Profiles(t);
    this.sessions = new Sessions(t);
    this.files = new Files(t);
    this.usage = new Usage(t);
    this.admin = new Admin(t);
  }
}
