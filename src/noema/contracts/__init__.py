"""The vendored rev-6 contracts and everything derived from them (IMPL-041).

- `_vendor/`: byte copy of docs/noema/contracts (`tools/sync_contracts.py --check` enforces identity);
- `build/openapi.bundled.yaml`: the Redocly bundle, input of the gate, the validator and the generators;
- `_generated/models.py`: strict pydantic v2 server models (`StrictBase`);
- `gate.RequestGate`: the only request accept/reject authority (jsonschema-rs, per operation);
- `rules`: the vendored semantic request invariants mapped to `NoemaError`;
- `validate.ContractValidator`: the test-mode response and SSE validator (NOEMA_CONTRACT_VALIDATE=1, never prod).

`noema.contracts` imports only `noema.core` (spec-01 §2 rule 2).
"""
