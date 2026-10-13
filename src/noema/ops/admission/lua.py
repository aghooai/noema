"""The admission Lua scripts (spec-11 §1.2). Each runs atomically in Redis/Valkey, so a reservation debits all of
its buckets and takes all of its slots, or changes nothing. Semantics are exactly `gcra.LocalStore`'s.

Numbers cross the boundary as strings: Python sends `repr(float)` (shortest round-trip) and the scripts store and
return TATs with `%.17g`, so no precision is lost on either side. Every key is passed in `KEYS` (cluster-safe
declaration; multi-key atomicity needs a non-cluster deployment or one hash slot).
"""

from __future__ import annotations

from typing import Final

# Debit markers (Wave E): a writing reserve may pass one more key, its marker. The script refuses to run if the
# marker exists (status 3: this debit was already undone by `REFUND` with the same marker, e.g. a late duplicate),
# and on a write sets it to '1' for MARKER_TTL_MS. `REFUND` with the marker refunds the buckets only if it is '1',
# then sets it to 'x': undoing a reserve whose reply was lost is exactly-once whether or not, and whenever, it ran.
MARKER_TTL_MS: Final = 60_000  # the scripts' literal 60000

# KEYS: nb bucket keys, then ns slot keys, then the optional marker.
# ARGV: now_ms, nb, ns, lease_ms, check; per bucket: interval_ms, n, threshold_ms, slices; per slot: cap, count,
# member x count. slices = 0: a GCRA bucket (a string TAT). slices > 0: a window bucket (gcra.QUOTA_SLICES; a hash of
# slice index -> units) with interval_ms = the slice width and threshold_ms = the cap in units.
# Returns {status, full_slot, then per bucket: before, after, wait_ms, reset_ms}: status 0 ok (written), 1 a bucket
# denied, 2 a slot full, 3 the marker exists (nothing written). check = 1 evaluates the buckets only and writes nothing
# (status 0 = they would pass; slots are not looked at). wait_ms / reset_ms are 0 for GCRA buckets.
RESERVE: Final = """
local now = tonumber(ARGV[1])
local nb, ns, lease, check = tonumber(ARGV[2]), tonumber(ARGV[3]), ARGV[4], ARGV[5]
local marker = KEYS[nb + ns + 1]
if marker and redis.call('EXISTS', marker) == 1 then return {3, 0} end
local function fmt(x) return string.format('%.17g', x) end
local out, after, counts, wins = {0, 0}, {}, {}, {}
local denied = false
local a = 6
for i = 1, nb do
  local interval, n, threshold = tonumber(ARGV[a]), tonumber(ARGV[a + 1]), tonumber(ARGV[a + 2])
  local slices = tonumber(ARGV[a + 3])
  a = a + 4
  counts[i] = n
  if slices == 0 then
    local tat = tonumber(redis.call('GET', KEYS[i]) or ARGV[1])
    if tat < now then tat = now end
    local nt = tat + n * interval
    after[i] = nt
    if nt - now > threshold then denied = true end
    out[#out + 1] = fmt(tat)
    out[#out + 1] = fmt(nt)
    out[#out + 1] = '0'
    out[#out + 1] = '0'
  else
    local cur = math.floor(now / interval)
    local flat = redis.call('HGETALL', KEYS[i])
    local used, live, stale, newest = 0, {}, {}, nil
    for j = 1, #flat, 2 do
      local idx, c = tonumber(flat[j]), tonumber(flat[j + 1])
      if idx > cur - slices then
        used = used + c
        live[#live + 1] = {idx, c}
        if newest == nil or idx > newest then newest = idx end
      else
        stale[#stale + 1] = flat[j]
      end
    end
    local wait, reset = 0, 0
    if newest then reset = (newest + slices) * interval - now end
    if used + n > threshold then
      denied = true
      if n <= threshold then
        table.sort(live, function(x, y) return x[1] < y[1] end)
        local left = used
        for _, e in ipairs(live) do
          left = left - e[2]
          if left + n <= threshold then
            wait = (e[1] + slices) * interval - now
            break
          end
        end
      end
    end
    wins[i] = {string.format('%.0f', cur), stale, math.ceil(slices * interval) + 1000, ARGV[a - 3]}
    out[#out + 1] = fmt(used)
    out[#out + 1] = fmt(used + n)
    out[#out + 1] = fmt(wait)
    out[#out + 1] = fmt(reset)
  end
end
if denied then
  out[1] = 1
  return out
end
if check == '1' then return out end
local slot_args = {}
for j = 1, ns do
  local key = KEYS[nb + j]
  local cap, count = tonumber(ARGV[a]), tonumber(ARGV[a + 1])
  slot_args[j] = {a + 2, count}
  a = a + 2 + count
  redis.call('ZREMRANGEBYSCORE', key, '-inf', ARGV[1])
  if redis.call('ZCARD', key) + count > cap then
    out[1], out[2] = 2, j - 1
    return out
  end
end
for i = 1, nb do
  local w = wins[i]
  if counts[i] > 0 and w then
    if #w[2] > 0 then redis.call('HDEL', KEYS[i], unpack(w[2])) end
    redis.call('HINCRBY', KEYS[i], w[1], w[4])
    redis.call('PEXPIRE', KEYS[i], w[3])
  elseif counts[i] > 0 then
    redis.call('SET', KEYS[i], fmt(after[i]), 'PX', math.ceil(after[i] - now) + 1000)
  end
end
local ttl = math.ceil(tonumber(lease) - now) + 1000
for j = 1, ns do
  local key, first, count = KEYS[nb + j], slot_args[j][1], slot_args[j][2]
  for c = 0, count - 1 do
    redis.call('ZADD', key, lease, ARGV[first + c])
  end
  if redis.call('PTTL', key) < ttl then redis.call('PEXPIRE', key, ttl) end
end
if marker then redis.call('SET', marker, '1', 'PX', 60000) end
return out
"""

# KEYS: nb bucket keys, then ns slot keys, then the optional marker of the reserve being undone.
# ARGV: now_ms, nb, ns; per bucket: interval_ms, r, slices, at_ms (a window bucket gives r units back to the slice of
# at_ms, the time of its debit); per slot: count, member x count. Returns nb.
REFUND: Final = """
local now = tonumber(ARGV[1])
local nb, ns = tonumber(ARGV[2]), tonumber(ARGV[3])
local a = 4
local marker = KEYS[nb + ns + 1]
local applied = true
if marker then
  applied = redis.call('GET', marker) == '1'
  redis.call('SET', marker, 'x', 'PX', 60000)
end
for i = 1, nb do
  local interval, r, slices, at = tonumber(ARGV[a]), tonumber(ARGV[a + 1]), tonumber(ARGV[a + 2]), tonumber(ARGV[a + 3])
  a = a + 4
  if slices > 0 then
    local field = string.format('%.0f', math.floor(at / interval))
    local v = applied and r > 0 and redis.call('HGET', KEYS[i], field)
    if v then
      local left = tonumber(v) - r
      if left > 0 then
        redis.call('HSET', KEYS[i], field, string.format('%.0f', left))
      else
        redis.call('HDEL', KEYS[i], field)
      end
    end
  end
  local v = slices == 0 and redis.call('GET', KEYS[i])
  if applied and v and r > 0 then
    local tat = tonumber(v) - r * interval
    if tat > now then
      redis.call('SET', KEYS[i], string.format('%.17g', tat), 'PX', math.ceil(tat - now) + 1000)
    else
      redis.call('DEL', KEYS[i])
    end
  end
end
for j = 1, ns do
  local count = tonumber(ARGV[a])
  for c = 1, count do
    redis.call('ZREM', KEYS[nb + j], ARGV[a + c])
  end
  a = a + 1 + count
end
return nb
"""
