import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.blobstore import BlobStore
from app.cache import UnlockedKeys
from app.config import Settings, get_settings
from app.crypto import UserCipher
from app.models import Vault
from app.sessions import SessionUser


async def get_db(request: Request) -> AsyncSession:
    async with request.app.state.sessionmaker() as session:
        yield session


def get_keys(request: Request) -> UnlockedKeys:
    return request.app.state.keys


def get_blobs(request: Request) -> BlobStore:
    return request.app.state.blobs


SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[AsyncSession, Depends(get_db)]
KeysDep = Annotated[UnlockedKeys, Depends(get_keys)]
BlobsDep = Annotated[BlobStore, Depends(get_blobs)]


async def require_service_token(
    settings: SettingsDep, authorization: Annotated[str | None, Header()] = None
) -> None:
    """Only storage-ui and clusterkeep-ui hold this token; a browser or another pod can't call storage-api directly."""
    presented = (authorization or "").removeprefix("Bearer ").strip()
    if not settings.service_token or not secrets.compare_digest(presented, settings.service_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid service token", headers={"WWW-Authenticate": "Bearer"})


async def require_user(request: Request, settings: SettingsDep) -> SessionUser:
    token = request.cookies.get(settings.session_cookie_name, "")
    user = await request.app.state.sessions.verify(token)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not_signed_in")
    return user


UserDep = Annotated[SessionUser, Depends(require_user)]

ADMIN_GROUPS = ("cluster-admins", "admin")


async def require_admin_user(user: UserDep) -> SessionUser:
    if not any(group in ADMIN_GROUPS for group in user.groups):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin_only")
    return user


class Unlocked:
    def __init__(self, user: SessionUser, vault: Vault, cipher: UserCipher) -> None:
        self.user = user
        self.vault = vault
        self.cipher = cipher

    @property
    def user_id(self) -> str:
        return self.user.user_id


async def require_unlocked(user: UserDep, db: DbDep, keys: KeysDep) -> Unlocked:
    vault = await db.get(Vault, user.user_id)
    if vault is None:
        raise HTTPException(status.HTTP_423_LOCKED, "not_set_up")
    data_key = await keys.get(user.session_token, user.user_id)
    if data_key is None:
        raise HTTPException(status.HTTP_423_LOCKED, "locked")
    return Unlocked(user, vault, UserCipher(data_key))


UnlockedDep = Annotated[Unlocked, Depends(require_unlocked)]
