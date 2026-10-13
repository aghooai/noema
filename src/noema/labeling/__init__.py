"""noema.labeling (spec-01 §1; ADR-003): sampling, D4 labeling jobs, calibrator fitting, the acceptance gate, drift
and the per-pod calibrator registry. Public entry points:

- `Sampler` (`LabelingSampler`) and `DbCalibratorRegistry` (`CalibratorRegistry`), built by the composition root;
- `make_handlers(services)` for the `jobs` process (`labeling.item`, `labeling.run`, `labeling.drift`);
- `views` for the W4 admin routes.
"""

from noema.labeling.jobs import DRIFT_PERIOD_S, make_handlers
from noema.labeling.registry import DbCalibratorRegistry
from noema.labeling.sampler import Sampler

__all__ = ["DRIFT_PERIOD_S", "DbCalibratorRegistry", "Sampler", "make_handlers"]
