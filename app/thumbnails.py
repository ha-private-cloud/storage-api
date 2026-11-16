import io
import logging
import shutil
import subprocess

from PIL import Image, ImageOps

from app.blobstore import BlobStore
from app.config import Settings
from app.crypto import UserCipher
from app.models import Node

logger = logging.getLogger("storage-api")

THUMBNAIL_SIZE = (480, 480)


def _image_thumbnail(source: bytes) -> bytes:
    with Image.open(io.BytesIO(source)) as image:
        image = ImageOps.exif_transpose(image)
        image.thumbnail(THUMBNAIL_SIZE)
        out = io.BytesIO()
        image.convert("RGB").save(out, format="JPEG", quality=80)
        return out.getvalue()


def _video_poster(source: bytes) -> bytes | None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-frames:v", "1",
         "-vf", f"scale={THUMBNAIL_SIZE[0]}:-2", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
        input=source,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return None
    return _image_thumbnail(result.stdout)


def build(blobs: BlobStore, settings: Settings, node: Node, cipher: UserCipher) -> bool:
    """Decrypts in memory only; plaintext never touches the disk."""
    mime = node.mime_type or ""
    try:
        if mime.startswith("image/") and 0 < node.size_bytes <= settings.thumbnail_max_source_bytes:
            source = b"".join(blobs.read_range(node.user_id, node.id, cipher.file_key(node.id), 0, node.size_bytes - 1))
            jpeg = _image_thumbnail(source)
        elif mime.startswith("video/") and node.size_bytes > 0:
            end = min(node.size_bytes, settings.video_poster_read_bytes) - 1
            source = b"".join(blobs.read_range(node.user_id, node.id, cipher.file_key(node.id), 0, end))
            jpeg = _video_poster(source)
        else:
            return False
    except Exception:
        logger.info("no thumbnail for %s", node.id)
        return False
    if not jpeg:
        return False
    blobs.write_thumbnail(node.user_id, node.id, cipher, jpeg)
    return True
