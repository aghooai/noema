"""Apply E sprint A's createTurn change (TurnService.create) to a snapshot's base routes/turns.py."""
import sys
from pathlib import Path

p = Path(sys.argv[1]) / "src/noema/api/routes/turns.py"
s = p.read_text()
old = '''    body = parse_turn_request(ctx.json)
    authorized = await service.authorize(who, body)
    turn_id = svc.ids.new("turn")
    replay = await deps.begin_idempotency(request, resource_id=turn_id, stream=body.stream)
    if replay is not None:
        return replay
    deadline = ctx.deadline
    assert deadline is not None  # createTurn always carries Aghoo-Deadline-Ms (default 120 s)
    prepared = await service.prepare(
        snapshot=ctx.snapshot,
        principal=who,
        request=body,
        authorized=authorized,
        turn_id=turn_id,
        t_in=ctx.t_in,
        deadline=deadline,
        trace_id=ctx.trace.trace_id or "0" * 32,
    )
'''
new = '''    body = parse_turn_request(ctx.json)
    turn_id = svc.ids.new("turn")
    deadline = ctx.deadline
    assert deadline is not None  # createTurn always carries Aghoo-Deadline-Ms (default 120 s)
    created = await service.create(
        snapshot=ctx.snapshot,
        principal=who,
        request=body,
        turn_id=turn_id,
        t_in=ctx.t_in,
        deadline=deadline,
        trace_id=ctx.trace.trace_id or "0" * 32,
        begin=lambda: deps.begin_idempotency(request, resource_id=turn_id, stream=body.stream),
        keyed=ctx.idempotency is not None and ctx.idempotency.key is not None,
    )
    if isinstance(created, Response):
        return created
    prepared = created
'''
assert old in s
p.write_text(s.replace(old, new))
print("patched", p)
