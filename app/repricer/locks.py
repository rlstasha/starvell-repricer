import secrets

from redis.asyncio import Redis


class RedisPositionLock:
    def __init__(
        self,
        redis: Redis,
        *,
        ttl_seconds: int,
        owner: str | None = None,
        key_prefix: str = "repricer:lock:position",
    ):
        self.redis = redis
        self.ttl_seconds = ttl_seconds
        self.owner = owner or secrets.token_hex(8)
        self.key_prefix = key_prefix
        self._owned_keys: set[str] = set()

    async def acquire(self, position_amount: int) -> bool:
        key = self.key(position_amount)
        acquired = await self.redis.set(
            key,
            self.owner,
            ex=self.ttl_seconds,
            nx=True,
        )
        if bool(acquired):
            self._owned_keys.add(key)
            return True
        return False

    async def release(self, position_amount: int) -> None:
        key = self.key(position_amount)
        script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        end
        return 0
        """
        await self.redis.eval(script, 1, key, self.owner)
        self._owned_keys.discard(key)

    def key(self, position_amount: int) -> str:
        return f"{self.key_prefix}:{position_amount}"
