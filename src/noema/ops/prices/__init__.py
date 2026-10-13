"""noema.ops.prices (IMPL-019, spec-05 §2): operator tooling for price catalogs (a placeholder generator). Pricing
itself is `noema.ops.cost.LedgerCostMeter` over the snapshot's `PriceCatalog`."""

from noema.ops.prices.placeholder import COMPONENTS, home_regions, placeholder_catalog, price_keys, render

__all__ = ["COMPONENTS", "home_regions", "placeholder_catalog", "price_keys", "render"]
