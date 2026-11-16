import hashlib

import redis.asyncio as aioredis

from app.config import Settings
from app.crypto import DecryptionFailed, KeyCacheSealer


def digest(value: str) -> str:
    return hashlib.sha3_512(value.encode("utf-8")).hexdigest()


class UnlockedKeys:
    """Unlocked data keys, held per sign-in session and dropped on lock or when the session expires."""

    def __init__(self, settings: Settings, client: aioredis.Redis) -> None:
        self._settings = settings
        self._client = client
        self._sealer = KeyCacheSealer(settings.key_cache_secret)

    def _key(self, session_token: str) -> str:
        return f"{self._settings.redis_namespace}:unlocked:{digest(session_token)}"

    async def put(self, session_token: str, user_id: str, data_key: bytes) -> None:
        await self._client.set(
            self._key(session_token),
            user_id.encode() + b"|" + self._sealer.seal(user_id, data_key),
            ex=self._settings.unlocked_key_seconds,
        )

    async def get(self, session_token: str, user_id: str) -> bytes | None:
        raw = await self._client.get(self._key(session_token))
        if raw is None:
            return None
        stored_user, _, sealed = bytes(raw).partition(b"|")
        if stored_user.decode() != user_id:
            return None
        try:
            return self._sealer.open(user_id, sealed)
        except DecryptionFailed:
            return None

    async def drop(self, session_token: str) -> None:
        await self._client.delete(self._key(session_token))

    async def ping(self) -> bool:
        return bool(await self._client.ping())


def build_client(settings: Settings) -> aioredis.Redis:
    return aioredis.from_url(settings.redis_url, decode_responses=False)
