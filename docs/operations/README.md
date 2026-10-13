# Operations

For operators of a Noema deployment. Phase 1 ships container images and a compose stack; it ships no production
infrastructure code (no Kubernetes manifests, no AWS IaC). These documents give what a production deployment has to
reproduce and how to operate it.

| Document | Contents |
|---|---|
| [deployment.md](deployment.md) | images, the compose `app` profile, process environment, secrets, migrations, health and graceful shutdown, Postgres and Valkey in production (backups, restore, eviction, persistence) |
| [configuration.md](configuration.md) | the platform config, two-person activation and rollback, prices, tenants, projects, principals and API keys with `noema-admin` |
| [tenant-onboarding.md](tenant-onboarding.md) | onboarding a tenant AWS account (IaC steps and `noema-admin` steps), ExternalId rotation, offboarding |
| [sizing.md](sizing.md) | pod sizing from the measured knee, scale signals, datastore connections |
| [observability.md](observability.md) | the Prometheus rules, alerts and Grafana dashboards |
| [runbooks.md](runbooks.md) | one runbook per alert and per design runbook: symptoms, checks, commands, mitigations, escalation |
| [upgrades-and-migrations.md](upgrades-and-migrations.md) | upgrade order, database migrations, config and price changes, rollback |
| [troubleshooting.md](troubleshooting.md) | error codes and symptoms mapped to causes and fixes |

Conventions in these documents:
- `<project>` is a compose project (the e2e stack is `noema-e2e`); `C="docker compose -p <project> -f
  deploy/compose/docker-compose.yml"` is used in commands.
- `noema-admin` is `uv run --frozen --no-sync python -m noema.identity.cli` from a checkout, or
  `python -m noema.identity.cli` inside an image. Its environment is in [configuration.md](configuration.md) section 1.
- Numbers come from the reports they cite; a command marked "not run" was not executed while writing these docs.
