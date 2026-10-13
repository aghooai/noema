"""The compose file: profiles, loopback-only ports and the ADR-002 §2 container privileges (rendered by compose)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from tests.integration.infra.helpers import BASE_SERVICES, COMPOSE_FILE, PORT_VARIABLES, SANDBOX_DIR, Compose

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("docker")]

PROFILE_SERVICES = {
    "toxiproxy": {"toxiproxy-host"},
    "media": {"media-sandbox"},
    "app": {"api-1", "api-2", "jobs", "media-worker", "media-sandbox", "mockbedrock", "toxiproxy"},
    "fault": {"toxiproxy"},
}
SANDBOX_CAPS = {"SYS_ADMIN", "SETUID", "SETGID", "CHOWN", "DAC_OVERRIDE", "FOWNER", "KILL"}
GIB = 1024**3


def _all(compose: Compose) -> dict[str, Any]:
    services: dict[str, Any] = compose.config("*")["services"]
    return services


def test_default_profile_starts_only_the_base_services(compose_config: Compose) -> None:
    assert set(compose_config.config()["services"]) == BASE_SERVICES


@pytest.mark.parametrize("profile", sorted(PROFILE_SERVICES))
def test_each_profile_adds_its_services(compose_config: Compose, profile: str) -> None:
    services = set(compose_config.config(profile)["services"])
    assert services - BASE_SERVICES == PROFILE_SERVICES[profile]


def test_every_published_port_binds_loopback_only(compose_config: Compose) -> None:
    for name, svc in _all(compose_config).items():
        for port in svc.get("ports") or []:
            assert port["host_ip"] == "127.0.0.1", (name, port)


def test_profiled_services_publish_ephemeral_ports_by_default(compose_config: Compose) -> None:
    for name, svc in _all(compose_config).items():
        if name not in BASE_SERVICES:
            assert all(p["published"] == "0" for p in svc.get("ports") or []), name


def test_an_isolated_project_can_make_every_host_port_ephemeral() -> None:
    stack = Compose(project="noema-infra-render", variables=dict.fromkeys(PORT_VARIABLES, "0"))
    for name, svc in _all(stack).items():
        assert all(p["published"] == "0" for p in svc.get("ports") or []), name


def test_media_sandbox_has_the_adr002_privileges(compose_config: Compose) -> None:
    svc = _all(compose_config)["media-sandbox"]
    assert svc["network_mode"] == "none"
    assert svc["cap_drop"] == ["ALL"]
    assert set(svc["cap_add"]) == SANDBOX_CAPS
    assert svc["security_opt"] == ["seccomp=../sandbox/docker-seccomp.json", "no-new-privileges:true"]
    assert (COMPOSE_FILE.parent / "../sandbox/docker-seccomp.json").resolve() == SANDBOX_DIR / "docker-seccomp.json"
    assert svc["read_only"] is True
    assert int(svc["mem_limit"]) == 6 * GIB
    assert float(svc["cpus"]) == 2
    assert svc["pids_limit"] == 1024
    assert "ports" not in svc and "user" not in svc  # sandboxd runs as container root; jails as 100000+slot
    assert [(v["source"], v["target"]) for v in svc["volumes"]] == [("media-spool", "/spool")]


def test_media_sandbox_gets_no_credentials(compose_config: Compose) -> None:
    env = _all(compose_config)["media-sandbox"]["environment"]
    assert set(env) == {"NOEMA_ENV"}
    assert env["NOEMA_ENV"] != "prod"


def test_media_worker_has_the_adr002_privileges(compose_config: Compose) -> None:
    svc = _all(compose_config)["media-worker"]
    assert svc["image"] == "noema-worker:dev"
    assert svc["user"] == "10001:10002"
    assert svc["cap_drop"] == ["ALL"]
    assert svc["read_only"] is True
    assert int(svc["mem_limit"]) == 1 * GIB
    assert svc["pids_limit"] == 256
    assert [(v["source"], v["target"]) for v in svc["volumes"]] == [("media-spool", "/spool")]
    assert svc["depends_on"]["media-sandbox"]["condition"] == "service_healthy"


def test_media_spool_is_a_worker_owned_tmpfs(compose_config: Compose) -> None:
    opts = compose_config.config("*")["volumes"]["media-spool"]["driver_opts"]
    assert opts["type"] == "tmpfs"
    assert dict(o.split("=", 1) for o in opts["o"].split(",")) == {
        "size": "4g",
        "uid": "10001",
        "gid": "10002",
        "mode": "0711",
    }


def test_the_mockbedrock_healthcheck_is_the_o1_form(compose_config: Compose) -> None:
    # the unpaged GET /_mock/requests serializes the whole record ring on the mock's event loop (spec-10 §5 lag)
    test = _all(compose_config)["mockbedrock"]["healthcheck"]["test"]
    assert test[:3] == ["CMD", "python", "-I"]
    assert "'http://127.0.0.1:8080/_mock/requests?limit=0'" in test[-1]


def test_app_services_only_reach_local_endpoints(compose_config: Compose) -> None:
    services = _all(compose_config)
    for name in ("api-1", "api-2", "jobs", "media-worker"):
        env = services[name]["environment"]
        overrides = json.loads(env["NOEMA_AWS_ENDPOINT_OVERRIDES"])
        assert set(overrides) == {"sts", "secretsmanager", "s3", "bedrock-runtime", "mantle"}, name
        assert all(url.startswith(("http://moto:5000", "http://mockbedrock:8080")) for url in overrides.values())
        assert overrides["mantle"].endswith("/v1")
        assert env["AWS_ACCESS_KEY_ID"] == "noema-compose-fake"
        assert env["NOEMA_ENV"] in {"test", "dev"}
        assert env["NOEMA_CONTRACT_VALIDATE"] is None  # passed through from the caller (`1` or unset)
        assert services[name]["user"] == "10001:10002" and services[name]["cap_drop"] == ["ALL"]


def test_toxiproxy_fault_proxies_front_the_base_services(compose_config: Compose) -> None:
    proxies = json.loads((COMPOSE_FILE.parent / "toxiproxy.json").read_text())
    upstreams = {p["name"]: p["upstream"] for p in proxies}
    assert upstreams == {"postgres": "postgres:5432", "valkey": "valkey:6379", "moto": "moto:5000"}
    published = {p["target"] for p in _all(compose_config)["toxiproxy"]["ports"]}
    assert published == {8474} | {int(p["listen"].rsplit(":", 1)[1]) for p in proxies}


def test_host_toxiproxy_uses_the_host_network_on_loopback(compose_config: Compose) -> None:
    svc = _all(compose_config)["toxiproxy-host"]
    assert svc["network_mode"] == "host"
    assert svc["command"] == ["-host=127.0.0.1", "-port=28474"]


def test_outer_seccomp_profile_is_docker_default_plus_pivot_root() -> None:
    profile = json.loads((SANDBOX_DIR / "docker-seccomp.json").read_text())
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    rules = [r for r in profile["syscalls"] if "pivot_root" in r["names"]]
    assert rules == [{"names": ["pivot_root"], "action": "SCMP_ACT_ALLOW", "includes": {"caps": ["CAP_SYS_ADMIN"]}}]


def test_the_app_services_run_their_processes_with_graceful_stops(compose_config: Compose) -> None:
    services = _all(compose_config)
    assert services["media-worker"]["command"][-2:] == ["--kinds", "media.process,file.purge"]  # its kinds only
    assert "media.process" not in " ".join(services["jobs"]["command"])
    assert {services[n]["hostname"] for n in ("api-1", "api-2", "jobs", "media-worker")} == {
        "api-1",
        "api-2",
        "jobs",
        "media-worker",
    }  # the pod ids
    for name in ("jobs", "media-worker"):
        assert services[name]["stop_grace_period"] == "35s"  # 10 s handlers + 10 s flush + 10 s cancelled worker
    for name in ("api-1", "api-2", "jobs", "media-worker"):
        assert services[name]["depends_on"]["toxiproxy"]["condition"] == "service_healthy"  # fault runs reuse it


def test_mockbedrock_starts_with_the_stack_scenario(compose_config: Compose) -> None:
    svc = _all(compose_config)["mockbedrock"]
    assert svc["command"][-2:] == ["--scenario", "/opt/noema-tools/scenarios/default.yaml"]
    mounts = {(v["source"].rsplit("/", 1)[-1], v["target"], v.get("read_only")) for v in svc["volumes"]}
    assert ("mockbedrock", "/opt/noema-tools/scenarios", True) in mounts
    assert (COMPOSE_FILE.parent / "mockbedrock" / "default.yaml").is_file()


def test_fault_runs_can_route_the_app_through_toxiproxy() -> None:
    stack = Compose(
        project="noema-infra-render",
        variables={
            "NOEMA_STACK_REDIS_URL": "redis://toxiproxy:16379/0",
            "NOEMA_STACK_MOTO_URL": "http://toxiproxy:15000",
        },
    )
    env = _all(stack)["api-1"]["environment"]
    assert env["NOEMA_REDIS_URL"] == "redis://toxiproxy:16379/0"
    overrides = json.loads(env["NOEMA_AWS_ENDPOINT_OVERRIDES"])
    assert {overrides[k] for k in ("sts", "secretsmanager", "s3")} == {"http://toxiproxy:15000"}


def test_an_api_pod_runs_two_worker_processes_on_two_cpus(compose_config: Compose) -> None:
    services = _all(compose_config)
    for name in ("api-1", "api-2"):
        svc = services[name]
        assert svc["environment"]["WEB_CONCURRENCY"] == "2"  # uvicorn --workers (E sprint B, 2 x 100 streams)
        assert svc["environment"]["NOEMA_MAX_TURNS_PER_PROCESS"] == "120"
        assert svc["environment"]["NOEMA_METRICS_PORT"] == "9464"  # the shared anchor's environment is kept
        assert svc["cpus"] == 2
        assert {p["target"] for p in svc["ports"]} == {8000, 9464, 9465}  # one metrics port per worker
    for name in ("jobs", "media-worker"):
        assert "WEB_CONCURRENCY" not in services[name]["environment"]
