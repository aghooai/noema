"""noema.turns (spec-01 §1): the turn orchestrator of spec-04.

Entry point: `noema.turns.service.TurnService` (one per process, `TurnService.of(services)`), used by
`noema.api.routes.turns`. The turn task is `noema.turns.runner.TurnRunner`; ingress steps 6 and 8-11 are in
`noema.turns.ingress`; see docs/modules/orchestrator.md.
"""
