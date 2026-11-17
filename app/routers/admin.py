from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.deps import DbDep, require_admin_user, require_service_token
from app.models import Vault

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["admin"],
    dependencies=[Depends(require_service_token), Depends(require_admin_user)],
)

MAX_QUOTA_BYTES = 100 * 1024**4


class QuotaUpdate(BaseModel):
    quota_bytes: int = Field(ge=0, le=MAX_QUOTA_BYTES)


def _out(vault: Vault) -> dict:
    return {"user_id": vault.user_id, "quota_bytes": vault.quota_bytes, "used_bytes": vault.used_bytes}


@router.get("/vaults")
async def list_vaults(db: DbDep) -> dict:
    vaults = await db.scalars(select(Vault).order_by(Vault.user_id))
    return {"items": [_out(v) for v in vaults]}


@router.put("/vaults/{user_id}/quota")
async def set_quota(user_id: str, body: QuotaUpdate, db: DbDep) -> dict:
    vault = await db.get(Vault, user_id)
    if vault is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not_found")
    vault.quota_bytes = body.quota_bytes
    await db.commit()
    return _out(vault)
