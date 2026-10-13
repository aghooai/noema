"""RedisKeys additions of C2a (spec-02 §13): the exact daily labeling cap (labeling.md item 5)."""

from __future__ import annotations

from noema.core.redis_keys import RedisKeys


def test_labeling_daily_is_per_tenant_and_day_inside_the_namespace() -> None:
    keys = RedisKeys("ns:agent:w0:abcd1234:")
    key = keys.labeling_daily("t_acme", "20261010")
    assert key == "ns:agent:w0:abcd1234:labeling:daily:t_acme:20261010"
    assert key != keys.labeling_daily("t_acme", "20261011") != keys.labeling_daily("t_b", "20261010")
