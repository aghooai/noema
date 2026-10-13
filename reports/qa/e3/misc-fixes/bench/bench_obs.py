import timeit
from noema.telemetry.setup import setup_telemetry
from noema.telemetry.otel import OtelInstruments
import inspect
s = setup_telemetry(service_name="b", env="dev")
print(inspect.signature(OtelInstruments.__init__))
from noema.telemetry import otel
strict = otel._Strictness(False) if hasattr(otel, "_Strictness") else None
ins = OtelInstruments(s.meter_provider.get_meter("x"), strict)
n = 200_000
t = timeit.timeit(lambda: ins.observe("aghoo_ttft_seconds", 0.012, profile="p", model="m", strategy="s"), number=n)
print(f"observe 3 labels: {t/n*1e6:.2f} us")
t = timeit.timeit(lambda: ins.observe("aghoo_inline_ingest_seconds", 0.012), number=n)
print(f"observe 0 labels: {t/n*1e6:.2f} us")
t = timeit.timeit(lambda: ins.set("aghoo_queue_depth", 3.0), number=n)
print(f"gauge set 0 labels: {t/n*1e6:.2f} us")
