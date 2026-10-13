"""Conformance suites (spec-02 §14): load the fake registrations and every owner `conformance_impls.py` before the
suite modules are collected (they parametrize over `registry.params(suite)` at import time)."""

from tests.contract_internal.registry import discover

discover()
