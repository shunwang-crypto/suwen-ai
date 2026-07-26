from __future__ import annotations

import time

from redis.asyncio import Redis

# Token bucket in a single atomic Lua script (per-key capacity + refill rate).
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_sec = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  ts = now
end
tokens = math.min(capacity, tokens + (now - ts) * refill_per_sec)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, 600)
return allowed
"""


class RateLimiter:
    def __init__(self, redis: Redis):
        self._redis = redis
        self._script = redis.register_script(_TOKEN_BUCKET_LUA)

    async def allow(self, key: str, per_minute: int) -> bool:
        allowed = await self._script(
            keys=[f"rl:{key}"],
            args=[per_minute, per_minute / 60.0, time.time()],
        )
        return int(allowed) == 1
