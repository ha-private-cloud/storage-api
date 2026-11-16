from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.blobstore import BlobStore
from app.crypto import UserCipher
from app.models import Node, Upload, Vault

MAX_NAME_LENGTH = 255
STALE_UPLOAD_AGE = timedelta(days=2)


SECTION_EXTENSIONS = {
    "photos": frozenset("jpg jpeg png gif webp heic heif avif bmp tif tiff dng".split()),
    "videos": frozenset("mp4 m4v mov webm mkv avi wmv mpg mpeg 3gp".split()),
    "music": frozenset("mp3 m4a aac flac wav ogg oga opus wma aif aiff".split()),
    "documents": frozenset(
        "pdf txt md rtf doc docx odt xls xlsx ods csv ppt pptx odp pages numbers key epub json xml".split()
    ),
}
SECTION_MIME_PREFIX = {"photos": "image/", "videos": "video/", "music": "audio/"}
MEDIA_MIME_PREFIXES = ("image/", "video/", "audio/")
GENERIC_MIME_TYPES = ("", "application/octet-stream")


class LibraryError(Exception):
    def __init__(self, status_code: int, detail: str, **extra) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.extra = extra


def clean_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > MAX_NAME_LENGTH or name in (".", ".."):
        raise LibraryError(400, "invalid_name")
    if "/" in name or "\\" in name or any(ord(ch) < 32 for ch in name):
        raise LibraryError(400, "invalid_name")
    return name


def type_allowed(section: str, name: str, mime: str | None) -> bool:
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension not in SECTION_EXTENSIONS[section]:
        return False
    mime = (mime or "").lower()
    if mime in GENERIC_MIME_TYPES:
        return True
    if section in SECTION_MIME_PREFIX:
        return mime.startswith(SECTION_MIME_PREFIX[section])
    return not mime.startswith(MEDIA_MIME_PREFIXES)


async def section_of(db: AsyncSession, folder_id: str | None) -> str | None:
    """The default folder a location sits inside, if any; its rule also covers every subfolder."""
    while folder_id is not None:
        folder = await db.get(Node, folder_id)
        if folder is None:
            return None
        if folder.section is not None:
            return folder.section
        folder_id = folder.parent_id
    return None


async def check_type(db: AsyncSession, parent_id: str | None, name: str, mime: str | None) -> None:
    section = await section_of(db, parent_id)
    if section is not None and not type_allowed(section, name, mime):
        raise LibraryError(400, "wrong_type", section=section)


async def check_subtree_type(db: AsyncSession, folder: Node, parent_id: str | None, cipher: UserCipher) -> None:
    section = await section_of(db, parent_id)
    if section is None:
        return
    pending = [folder]
    while pending:
        current = pending.pop()
        for child in await children(db, current.user_id, current.id):
            if child.kind == "folder":
                pending.append(child)
            elif not type_allowed(section, cipher.decrypt_name(child.id, child.encrypted_name), child.mime_type):
                raise LibraryError(400, "wrong_type", section=section)


def serialize(node: Node, cipher: UserCipher) -> dict:
    return {
        "id": node.id,
        "name": cipher.decrypt_name(node.id, node.encrypted_name),
        "kind": node.kind,
        "section": node.section,
        "parent_id": node.parent_id,
        "mime_type": node.mime_type,
        "size": node.size_bytes,
        "has_thumbnail": node.has_thumbnail,
        "modified_at": node.modified_at.isoformat(),
        "trashed_at": node.trashed_at.isoformat() if node.trashed_at else None,
    }


def sort_items(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda i: (i["kind"] != "folder", i["name"].casefold()))


async def get_node(db: AsyncSession, user_id: str, node_id: str, *, include_trashed: bool = False) -> Node:
    node = await db.get(Node, node_id)
    if node is None or node.user_id != user_id or (node.trashed_at is not None and not include_trashed):
        raise LibraryError(404, "not_found")
    if not include_trashed and await is_hidden(db, node):
        raise LibraryError(404, "not_found")
    return node


async def is_hidden(db: AsyncSession, node: Node) -> bool:
    """A node inside a trashed folder is hidden even though it isn't itself trashed."""
    parent_id = node.parent_id
    while parent_id is not None:
        parent = await db.get(Node, parent_id)
        if parent is None or parent.trashed_at is not None:
            return True
        parent_id = parent.parent_id
    return False


async def get_folder(db: AsyncSession, user_id: str, folder_id: str | None) -> Node | None:
    if folder_id is None:
        return None
    folder = await get_node(db, user_id, folder_id)
    if folder.kind != "folder":
        raise LibraryError(400, "not_a_folder")
    return folder


async def children(db: AsyncSession, user_id: str, parent_id: str | None) -> list[Node]:
    result = await db.scalars(
        select(Node).where(Node.user_id == user_id, Node.parent_id == parent_id, Node.trashed_at.is_(None))
    )
    return list(result)


async def ensure_unique(
    db: AsyncSession, user_id: str, parent_id: str | None, name: str, cipher: UserCipher, *, ignore_id: str | None = None
) -> None:
    wanted = name.casefold()
    for sibling in await children(db, user_id, parent_id):
        if sibling.id != ignore_id and cipher.decrypt_name(sibling.id, sibling.encrypted_name).casefold() == wanted:
            raise LibraryError(409, "name_taken")
    pending = await db.scalars(select(Upload).where(Upload.user_id == user_id, Upload.parent_id == (parent_id or "")))
    for upload in pending:
        if upload.id != ignore_id and cipher.decrypt_name(upload.id, upload.encrypted_name).casefold() == wanted:
            raise LibraryError(409, "name_taken")


async def breadcrumbs(db: AsyncSession, folder: Node | None, cipher: UserCipher) -> list[dict]:
    path = []
    while folder is not None:
        path.append({"id": folder.id, "name": cipher.decrypt_name(folder.id, folder.encrypted_name)})
        folder = await db.get(Node, folder.parent_id) if folder.parent_id else None
    return list(reversed(path))


async def is_descendant(db: AsyncSession, candidate_id: str | None, ancestor_id: str) -> bool:
    while candidate_id is not None:
        if candidate_id == ancestor_id:
            return True
        node = await db.get(Node, candidate_id)
        candidate_id = node.parent_id if node else None
    return False


async def all_user_nodes(db: AsyncSession, user_id: str) -> list[Node]:
    return list(await db.scalars(select(Node).where(Node.user_id == user_id)))


async def visible_nodes(db: AsyncSession, user_id: str) -> list[Node]:
    nodes = await all_user_nodes(db, user_id)
    by_id = {n.id: n for n in nodes}

    def visible(node: Node) -> bool:
        while node is not None:
            if node.trashed_at is not None:
                return False
            node = by_id.get(node.parent_id) if node.parent_id else None
        return True

    return [n for n in nodes if visible(n)]


async def delete_permanently(db: AsyncSession, blobs: BlobStore, node: Node) -> None:
    pending = [node]
    while pending:
        current = pending.pop()
        pending.extend(await db.scalars(select(Node).where(Node.parent_id == current.id)))
        if current.kind == "file":
            blobs.delete_file(current.user_id, current.id)
        await db.delete(current)


async def recompute_usage(db: AsyncSession, user_id: str) -> int:
    used = await db.scalar(
        select(func.coalesce(func.sum(Node.size_bytes), 0)).where(Node.user_id == user_id, Node.kind == "file")
    )
    vault = await db.get(Vault, user_id)
    if vault is not None:
        vault.used_bytes = int(used or 0)
    return int(used or 0)


async def pending_upload_bytes(db: AsyncSession, user_id: str) -> int:
    total = await db.scalar(
        select(func.coalesce(func.sum(Upload.size_bytes), 0)).where(Upload.user_id == user_id)
    )
    return int(total or 0)


async def purge_expired(db: AsyncSession, blobs: BlobStore, retention_days: int) -> int:
    now = datetime.now(timezone.utc)
    expired = list(await db.scalars(select(Node).where(Node.trashed_at < now - timedelta(days=retention_days))))
    users = set()
    for node in expired:
        users.add(node.user_id)
        await delete_permanently(db, blobs, node)
    for upload in list(await db.scalars(select(Upload).where(Upload.created_at < now - STALE_UPLOAD_AGE))):
        blobs.discard_upload(upload.user_id, upload.id)
        await db.delete(upload)
    await db.flush()
    for user_id in users:
        await recompute_usage(db, user_id)
    await db.commit()
    return len(expired)
