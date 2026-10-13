# Development

| Document | Contents |
|---|---|
| [setup.md](setup.md) | toolchain, dependencies, the dev services, a full local stack, a host-run API, the UI and the SDKs |
| [testing.md](testing.md) | test layers, `make check`, every QA target and its stack, determinism rules, the live smoke and its ledger |
| [../../CONTRIBUTING.md](../../CONTRIBUTING.md) | the contributing workflow: path ownership, frozen areas, change requests, what to run before a handoff |
| [../architecture.md](../architecture.md) | processes, module map and import layers, request lifecycle, data stores, deviations from the design |
| [../modules/](../modules/) | one handoff doc per module (its wiring, decisions and known limits), kept by the module's owner |

Ground rules that apply to every change: model calls go to Amazon Bedrock only (never the OpenAI API or any other
model API: `make forbidden-scan`); no dependency is added outside the platform role (`uv.lock`, `package-lock.json`);
tests never reach the network beyond loopback and the compose network; live Bedrock calls happen only in the
gated live smoke ([testing.md](testing.md) section 6).
