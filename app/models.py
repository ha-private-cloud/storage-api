import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Vault(Base):
    __tablename__ = "vaults"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    password_salt: Mapped[bytes] = mapped_column(LargeBinary)
    password_wrapped_key: Mapped[bytes] = mapped_column(LargeBinary)
    recovery_salt: Mapped[bytes] = mapped_column(LargeBinary)
    recovery_wrapped_key: Mapped[bytes] = mapped_column(LargeBinary)
    quota_bytes: Mapped[int] = mapped_column(BigInteger)
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("vaults.user_id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    kind: Mapped[str] = mapped_column(String(8))
    encrypted_name: Mapped[str] = mapped_column(Text)
    section: Mapped[str | None] = mapped_column(String(16), default=None)
    mime_type: Mapped[str | None] = mapped_column(String(255), default=None)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    has_thumbnail: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    modified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    trashed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None, index=True)


class Upload(Base):
    __tablename__ = "uploads"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("vaults.user_id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[str] = mapped_column(String(32))
    encrypted_name: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    chunk_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
