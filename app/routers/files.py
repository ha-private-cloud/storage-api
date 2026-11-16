import math
import re
from datetime import datetime, timezone
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app import library, thumbnails
from app.deps import BlobsDep, DbDep, SettingsDep, UnlockedDep, require_service_token
from app.library import LibraryError
from app.models import Node, Upload, new_id

router = APIRouter(prefix="/api/v1", tags=["files"], dependencies=[Depends(require_service_token)])

SECTIONS = ("photos", "videos", "music", "documents")
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
INLINE_TYPES = re.compile(r"^(image/(png|jpeg|gif|webp|avif|bmp)|video/[a-z0-9.+-]+|audio/[a-z0-9.+-]+)$")
CONTENT_POLICY = "sandbox; default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'"
MAX_MIME_LENGTH = 255


class FolderCreate(BaseModel):
    parent_id: str | None = None
    name: str = Field(min_length=1, max_length=library.MAX_NAME_LENGTH)


class NodeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=library.MAX_NAME_LENGTH)
    parent_id: str | None = None


class UploadCreate(BaseModel):
    parent_id: str | None = None
    name: str = Field(min_length=1, max_length=library.MAX_NAME_LENGTH)
    size: int = Field(ge=0)
    mime_type: str = Field(default="application/octet-stream", max_length=MAX_MIME_LENGTH)


def _error(exc: LibraryError) -> JSONResponse:
    return JSONResponse({"detail": exc.detail, **exc.extra}, status_code=exc.status_code)


async def _folder_listing(db, unlocked, folder: Node | None) -> dict:
    items = [library.serialize(n, unlocked.cipher) for n in await library.children(db, unlocked.user_id, folder.id if folder else None)]
    return {
        "folder": {
            "id": folder.id if folder else None,
            "name": unlocked.cipher.decrypt_name(folder.id, folder.encrypted_name) if folder else "My storage",
            "section": folder.section if folder else None,
            "path": await library.breadcrumbs(db, folder, unlocked.cipher),
        },
        "items": library.sort_items(items),
    }


@router.get("/usage")
async def usage(unlocked: UnlockedDep, db: DbDep) -> dict:
    used = await library.recompute_usage(db, unlocked.user_id)
    await db.commit()
    return {"used": used, "quota": unlocked.vault.quota_bytes}


@router.get("/folders")
async def list_folder(unlocked: UnlockedDep, db: DbDep, parent_id: str | None = None):
    try:
        folder = await library.get_folder(db, unlocked.user_id, parent_id)
    except LibraryError as exc:
        return _error(exc)
    return await _folder_listing(db, unlocked, folder)


@router.get("/sections/{section}")
async def list_section(section: str, unlocked: UnlockedDep, db: DbDep):
    if section not in SECTIONS:
        return JSONResponse({"detail": "not_found"}, status_code=404)
    folder = await db.scalar(select(Node).where(Node.user_id == unlocked.user_id, Node.section == section))
    if folder is None:
        return JSONResponse({"detail": "not_found"}, status_code=404)
    return await _folder_listing(db, unlocked, folder)


@router.get("/folder-tree")
async def folder_tree(unlocked: UnlockedDep, db: DbDep) -> dict:
    folders = [n for n in await library.visible_nodes(db, unlocked.user_id) if n.kind == "folder"]
    by_id = {f.id: f for f in folders}
    names = {f.id: unlocked.cipher.decrypt_name(f.id, f.encrypted_name) for f in folders}

    def path(folder: Node) -> str:
        parts = []
        while folder is not None:
            parts.append(names[folder.id])
            folder = by_id.get(folder.parent_id) if folder.parent_id else None
        return "/".join(reversed(parts))

    items = [{"id": f.id, "path": path(f)} for f in folders]
    return {"items": sorted(items, key=lambda i: i["path"].casefold())}


@router.get("/search")
async def search(unlocked: UnlockedDep, db: DbDep, q: Annotated[str, Query(min_length=1, max_length=255)]) -> dict:
    wanted = q.casefold()
    matches = []
    for node in await library.visible_nodes(db, unlocked.user_id):
        item = library.serialize(node, unlocked.cipher)
        if wanted in item["name"].casefold():
            matches.append(item)
    return {"items": library.sort_items(matches)}


@router.post("/folders", status_code=status.HTTP_201_CREATED)
async def create_folder(body: FolderCreate, unlocked: UnlockedDep, db: DbDep):
    try:
        name = library.clean_name(body.name)
        await library.get_folder(db, unlocked.user_id, body.parent_id)
        await library.ensure_unique(db, unlocked.user_id, body.parent_id, name, unlocked.cipher)
    except LibraryError as exc:
        return _error(exc)
    folder = Node(id=new_id(), user_id=unlocked.user_id, parent_id=body.parent_id, kind="folder", encrypted_name="")
    folder.encrypted_name = unlocked.cipher.encrypt_name(folder.id, name)
    db.add(folder)
    await db.commit()
    return library.serialize(folder, unlocked.cipher)


@router.patch("/nodes/{node_id}")
async def update_node(node_id: str, body: NodeUpdate, unlocked: UnlockedDep, db: DbDep):
    try:
        node = await library.get_node(db, unlocked.user_id, node_id)
        if node.section is not None:
            raise LibraryError(403, "default_folder")
        moving = "parent_id" in body.model_fields_set and body.parent_id != node.parent_id
        target_parent = body.parent_id if moving else node.parent_id
        if moving:
            await library.get_folder(db, unlocked.user_id, target_parent)
            if node.kind == "folder" and await library.is_descendant(db, target_parent, node.id):
                raise LibraryError(400, "cannot_move_into_itself")
        name = library.clean_name(body.name) if body.name is not None else unlocked.cipher.decrypt_name(node.id, node.encrypted_name)
        if node.kind == "file":
            await library.check_type(db, target_parent, name, node.mime_type)
        elif moving:
            await library.check_subtree_type(db, node, target_parent, unlocked.cipher)
        await library.ensure_unique(db, unlocked.user_id, target_parent, name, unlocked.cipher, ignore_id=node.id)
    except LibraryError as exc:
        return _error(exc)
    node.parent_id = target_parent
    node.encrypted_name = unlocked.cipher.encrypt_name(node.id, name)
    node.modified_at = datetime.now(timezone.utc)
    await db.commit()
    return library.serialize(node, unlocked.cipher)


@router.delete("/nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def trash_node(node_id: str, unlocked: UnlockedDep, db: DbDep):
    try:
        node = await library.get_node(db, unlocked.user_id, node_id)
        if node.section is not None:
            raise LibraryError(403, "default_folder")
    except LibraryError as exc:
        return _error(exc)
    node.trashed_at = datetime.now(timezone.utc)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/trash")
async def list_trash(unlocked: UnlockedDep, db: DbDep, settings: SettingsDep) -> dict:
    trashed = await db.scalars(
        select(Node).where(Node.user_id == unlocked.user_id, Node.trashed_at.is_not(None))
    )
    items = [library.serialize(n, unlocked.cipher) for n in trashed]
    return {"items": sorted(items, key=lambda i: i["trashed_at"], reverse=True), "retention_days": settings.trash_retention_days}


@router.post("/trash/{node_id}/restore")
async def restore(node_id: str, unlocked: UnlockedDep, db: DbDep):
    try:
        node = await library.get_node(db, unlocked.user_id, node_id, include_trashed=True)
        if node.trashed_at is None:
            raise LibraryError(404, "not_found")
        parent_ok = node.parent_id is None
        if not parent_ok:
            try:
                await library.get_folder(db, unlocked.user_id, node.parent_id)
                parent_ok = True
            except LibraryError:
                parent_ok = False
        target_parent = node.parent_id if parent_ok else None
        await library.ensure_unique(
            db, unlocked.user_id, target_parent, unlocked.cipher.decrypt_name(node.id, node.encrypted_name), unlocked.cipher, ignore_id=node.id
        )
    except LibraryError as exc:
        return _error(exc)
    node.parent_id = target_parent
    node.trashed_at = None
    await db.commit()
    return library.serialize(node, unlocked.cipher)


@router.delete("/trash/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_forever(node_id: str, unlocked: UnlockedDep, db: DbDep, blobs: BlobsDep):
    try:
        node = await library.get_node(db, unlocked.user_id, node_id, include_trashed=True)
        if node.trashed_at is None:
            raise LibraryError(404, "not_found")
    except LibraryError as exc:
        return _error(exc)
    await library.delete_permanently(db, blobs, node)
    await db.flush()
    await library.recompute_usage(db, unlocked.user_id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _quota_check(db, unlocked, extra: int) -> None:
    used = await library.recompute_usage(db, unlocked.user_id)
    pending = await library.pending_upload_bytes(db, unlocked.user_id)
    if used + pending + extra > unlocked.vault.quota_bytes:
        raise LibraryError(413, "quota_exceeded")


@router.post("/uploads", status_code=status.HTTP_201_CREATED)
async def start_upload(body: UploadCreate, unlocked: UnlockedDep, db: DbDep, settings: SettingsDep):
    try:
        name = library.clean_name(body.name)
        await library.get_folder(db, unlocked.user_id, body.parent_id)
        await library.check_type(db, body.parent_id, name, body.mime_type)
        await library.ensure_unique(db, unlocked.user_id, body.parent_id, name, unlocked.cipher)
        await _quota_check(db, unlocked, body.size)
    except LibraryError as exc:
        return _error(exc)
    upload = Upload(
        id=new_id(),
        user_id=unlocked.user_id,
        parent_id=body.parent_id or "",
        encrypted_name="",
        mime_type=body.mime_type or "application/octet-stream",
        size_bytes=body.size,
        chunk_count=max(1, math.ceil(body.size / settings.chunk_size)),
    )
    upload.encrypted_name = unlocked.cipher.encrypt_name(upload.id, name)
    db.add(upload)
    await db.commit()
    return {"id": upload.id, "chunk_size": settings.chunk_size, "chunk_count": upload.chunk_count, "received": []}


async def _get_upload(db, user_id: str, upload_id: str) -> Upload:
    upload = await db.get(Upload, upload_id)
    if upload is None or upload.user_id != user_id:
        raise LibraryError(404, "not_found")
    return upload


@router.get("/uploads/{upload_id}")
async def upload_status(upload_id: str, unlocked: UnlockedDep, db: DbDep, blobs: BlobsDep, settings: SettingsDep):
    try:
        upload = await _get_upload(db, unlocked.user_id, upload_id)
    except LibraryError as exc:
        return _error(exc)
    return {
        "id": upload.id,
        "chunk_size": settings.chunk_size,
        "chunk_count": upload.chunk_count,
        "received": blobs.received_chunks(unlocked.user_id, upload.id),
    }


@router.put("/uploads/{upload_id}/chunks/{index}", status_code=status.HTTP_204_NO_CONTENT)
async def upload_chunk(
    upload_id: str, index: int, request: Request, unlocked: UnlockedDep, db: DbDep, blobs: BlobsDep, settings: SettingsDep
):
    try:
        upload = await _get_upload(db, unlocked.user_id, upload_id)
        if not 0 <= index < upload.chunk_count:
            raise LibraryError(400, "invalid_chunk")
    except LibraryError as exc:
        return _error(exc)
    expected = min(settings.chunk_size, upload.size_bytes - index * settings.chunk_size)
    body = await request.body()
    if len(body) != max(expected, 0):
        return JSONResponse({"detail": "wrong_chunk_size", "expected": max(expected, 0)}, status_code=400)
    await run_in_threadpool(
        blobs.write_chunk, unlocked.user_id, upload.id, index, unlocked.cipher.file_key(upload.id), body
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/uploads/{upload_id}/complete", status_code=status.HTTP_201_CREATED)
async def complete_upload(
    upload_id: str,
    request: Request,
    unlocked: UnlockedDep,
    db: DbDep,
    blobs: BlobsDep,
    settings: SettingsDep,
    background: BackgroundTasks,
):
    try:
        upload = await _get_upload(db, unlocked.user_id, upload_id)
        received = blobs.received_chunks(unlocked.user_id, upload.id)
        if received != list(range(upload.chunk_count)):
            raise LibraryError(409, "missing_chunks")
        parent_id = upload.parent_id or None
        await library.get_folder(db, unlocked.user_id, parent_id)
        name = unlocked.cipher.decrypt_name(upload.id, upload.encrypted_name)
        await library.ensure_unique(db, unlocked.user_id, parent_id, name, unlocked.cipher, ignore_id=upload.id)
    except LibraryError as exc:
        return _error(exc)
    await run_in_threadpool(blobs.finish_upload, unlocked.user_id, upload.id)
    node = Node(
        id=upload.id,
        user_id=unlocked.user_id,
        parent_id=parent_id,
        kind="file",
        encrypted_name=upload.encrypted_name,
        mime_type=upload.mime_type,
        size_bytes=upload.size_bytes,
    )
    db.add(node)
    await db.delete(upload)
    await db.flush()
    await library.recompute_usage(db, unlocked.user_id)
    await db.commit()
    background.add_task(
        _make_thumbnail, request.app.state.sessionmaker, blobs, settings, node.id, unlocked.cipher
    )
    return library.serialize(node, unlocked.cipher)


async def _make_thumbnail(sessionmaker, blobs, settings, node_id: str, cipher) -> None:
    async with sessionmaker() as db:
        node = await db.get(Node, node_id)
        if node is None:
            return
        if await run_in_threadpool(thumbnails.build, blobs, settings, node, cipher):
            node.has_thumbnail = True
            await db.commit()


@router.delete("/uploads/{upload_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_upload(upload_id: str, unlocked: UnlockedDep, db: DbDep, blobs: BlobsDep):
    try:
        upload = await _get_upload(db, unlocked.user_id, upload_id)
    except LibraryError as exc:
        return _error(exc)
    blobs.discard_upload(unlocked.user_id, upload.id)
    await db.delete(upload)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    if not header:
        return None
    match = RANGE_RE.match(header.strip())
    if not match or (not match.group(1) and not match.group(2)):
        raise LibraryError(416, "invalid_range")
    if match.group(1):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else size - 1
    else:
        start = max(size - int(match.group(2)), 0)
        end = size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        raise LibraryError(416, "invalid_range")
    return start, end


@router.get("/files/{node_id}/content")
async def file_content(
    node_id: str,
    unlocked: UnlockedDep,
    db: DbDep,
    blobs: BlobsDep,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    download: bool = False,
):
    try:
        node = await library.get_node(db, unlocked.user_id, node_id)
        if node.kind != "file":
            raise LibraryError(400, "not_a_file")
        requested = _parse_range(range_header, node.size_bytes)
    except LibraryError as exc:
        headers = {"Content-Range": f"bytes */{node.size_bytes}"} if exc.status_code == 416 else None
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)

    name = unlocked.cipher.decrypt_name(node.id, node.encrypted_name)
    inline = not download and bool(INLINE_TYPES.match(node.mime_type or ""))
    media_type = node.mime_type if inline else "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(name)}",
        "Content-Security-Policy": CONTENT_POLICY,
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if node.size_bytes == 0:
        return Response(b"", media_type=media_type, headers=headers)
    start, end = requested or (0, node.size_bytes - 1)
    headers["Content-Length"] = str(end - start + 1)
    status_code = status.HTTP_200_OK
    if requested:
        status_code = status.HTTP_206_PARTIAL_CONTENT
        headers["Content-Range"] = f"bytes {start}-{end}/{node.size_bytes}"
    stream = blobs.read_range(unlocked.user_id, node.id, unlocked.cipher.file_key(node.id), start, end)
    return StreamingResponse(stream, status_code=status_code, media_type=media_type, headers=headers)


@router.get("/files/{node_id}/thumbnail")
async def file_thumbnail(node_id: str, unlocked: UnlockedDep, db: DbDep, blobs: BlobsDep):
    try:
        node = await library.get_node(db, unlocked.user_id, node_id)
    except LibraryError as exc:
        return _error(exc)
    jpeg = blobs.read_thumbnail(unlocked.user_id, node.id, unlocked.cipher) if node.has_thumbnail else None
    if jpeg is None:
        return JSONResponse({"detail": "not_found"}, status_code=404)
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
