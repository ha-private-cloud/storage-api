import time
from dataclasses import dataclass

import httpx

from app.cache import digest
from app.config import Settings


@dataclass(frozen=True)
class SessionUser:
    user_id: str
    username: str
    session_token: str


class SessionVerifier:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._recent: dict[str, tuple[float, SessionUser]] = {}

    async def verify(self, session_token: str) -> SessionUser | None:
        if not session_token:
            return None
        key = digest(session_token)
        cached = self._recent.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        try:
            response = await self._client.get(
                f"{self._settings.auth_api_internal_url.rstrip('/')}/session",
                cookies={self._settings.session_cookie_name: session_token},
                timeout=5,
            )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            self._recent.pop(key, None)
            return None
        body = response.json()
        user = SessionUser(user_id=body["sub"], username=body["preferred_username"], session_token=session_token)
        self._recent[key] = (time.monotonic() + self._settings.session_check_seconds, user)
        return user
