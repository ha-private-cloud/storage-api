from fastapi import APIRouter, Depends, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.crypto import (
    DecryptionFailed,
    UserCipher,
    derive_wrapping_key,
    new_key,
    new_recovery_code,
    new_salt,
    normalize_recovery_code,
    open_sealed,
    seal,
)
from app.deps import DbDep, KeysDep, SettingsDep, UserDep, require_service_token
from app.models import Node, Vault, new_id

router = APIRouter(prefix="/api/v1/vault", tags=["vault"], dependencies=[Depends(require_service_token)])

DEFAULT_FOLDERS = (("Photos", "photos"), ("Videos", "videos"), ("Music", "music"), ("Documents", "documents"))


class UnlockRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class RecoverRequest(BaseModel):
    recovery_code: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


def _aad(user_id: str, purpose: str) -> bytes:
    return f"{user_id}:{purpose}".encode()


async def _wrap(secret: str, data_key: bytes, user_id: str, purpose: str, settings) -> tuple[bytes, bytes]:
    salt = new_salt()
    wrapping_key = await run_in_threadpool(derive_wrapping_key, secret, salt, settings)
    return salt, seal(wrapping_key, data_key, _aad(user_id, purpose))


async def _unwrap(secret: str, salt: bytes, wrapped: bytes, user_id: str, purpose: str, settings) -> bytes | None:
    wrapping_key = await run_in_threadpool(derive_wrapping_key, secret, salt, settings)
    try:
        return open_sealed(wrapping_key, wrapped, _aad(user_id, purpose))
    except DecryptionFailed:
        return None


@router.get("")
async def vault_status(user: UserDep, db: DbDep, keys: KeysDep) -> dict:
    vault = await db.get(Vault, user.user_id)
    unlocked = vault is not None and await keys.get(user.session_token, user.user_id) is not None
    return {"exists": vault is not None, "unlocked": unlocked}


@router.post("/unlock")
async def unlock(body: UnlockRequest, user: UserDep, db: DbDep, keys: KeysDep, settings: SettingsDep) -> JSONResponse:
    vault = await db.get(Vault, user.user_id)
    if vault is None:
        data_key = new_key()
        recovery_code = new_recovery_code()
        password_salt, password_wrapped = await _wrap(body.password, data_key, user.user_id, "password", settings)
        recovery_salt, recovery_wrapped = await _wrap(recovery_code, data_key, user.user_id, "recovery", settings)
        db.add(
            Vault(
                user_id=user.user_id,
                password_salt=password_salt,
                password_wrapped_key=password_wrapped,
                recovery_salt=recovery_salt,
                recovery_wrapped_key=recovery_wrapped,
                quota_bytes=settings.default_quota_bytes,
                used_bytes=0,
            )
        )
        await db.flush()
        cipher = UserCipher(data_key)
        for name, section in DEFAULT_FOLDERS:
            folder_id = new_id()
            db.add(
                Node(
                    id=folder_id,
                    user_id=user.user_id,
                    kind="folder",
                    section=section,
                    encrypted_name=cipher.encrypt_name(folder_id, name),
                )
            )
        await db.commit()
        await keys.put(user.session_token, user.user_id, data_key)
        return JSONResponse({"status": "created", "recovery_code": recovery_code}, status_code=status.HTTP_201_CREATED)

    data_key = await _unwrap(
        body.password, vault.password_salt, vault.password_wrapped_key, user.user_id, "password", settings
    )
    if data_key is None:
        return JSONResponse({"detail": "recovery_required"}, status_code=status.HTTP_409_CONFLICT)
    await keys.put(user.session_token, user.user_id, data_key)
    return JSONResponse({"status": "unlocked"})


@router.post("/recover")
async def recover(body: RecoverRequest, user: UserDep, db: DbDep, keys: KeysDep, settings: SettingsDep) -> JSONResponse:
    vault = await db.get(Vault, user.user_id)
    if vault is None:
        return JSONResponse({"detail": "not_set_up"}, status_code=status.HTTP_404_NOT_FOUND)
    data_key = await _unwrap(
        normalize_recovery_code(body.recovery_code),
        vault.recovery_salt,
        vault.recovery_wrapped_key,
        user.user_id,
        "recovery",
        settings,
    )
    if data_key is None:
        return JSONResponse({"detail": "invalid_recovery_code"}, status_code=status.HTTP_400_BAD_REQUEST)
    vault.password_salt, vault.password_wrapped_key = await _wrap(
        body.password, data_key, user.user_id, "password", settings
    )
    await db.commit()
    await keys.put(user.session_token, user.user_id, data_key)
    return JSONResponse({"status": "unlocked"})


@router.post("/lock", status_code=status.HTTP_204_NO_CONTENT)
async def lock(user: UserDep, keys: KeysDep) -> Response:
    await keys.drop(user.session_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
