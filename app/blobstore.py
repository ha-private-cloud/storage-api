import os
import shutil
from collections.abc import Iterator
from pathlib import Path

from app.crypto import SEGMENT_OVERHEAD, UserCipher, decrypt_segment, encrypt_chunk, open_sealed, seal


class BlobStore:
    """Files are stored as encrypted chunks under an opaque id; nothing on disk carries a name or a user-visible path."""

    def __init__(self, root: str, chunk_size: int, segment_size: int) -> None:
        self.root = Path(root)
        self.chunk_size = chunk_size
        self.segment_size = segment_size

    def _user(self, user_id: str) -> Path:
        return self.root / user_id

    def _upload_dir(self, user_id: str, upload_id: str) -> Path:
        return self._user(user_id) / "uploads" / upload_id

    def _file_dir(self, user_id: str, node_id: str) -> Path:
        return self._user(user_id) / "files" / node_id

    def _thumbnail(self, user_id: str, node_id: str) -> Path:
        return self._user(user_id) / "thumbnails" / f"{node_id}.bin"

    def write_chunk(self, user_id: str, upload_id: str, chunk_index: int, file_key: bytes, plaintext: bytes) -> None:
        target = self._upload_dir(user_id, upload_id)
        target.mkdir(parents=True, exist_ok=True)
        partial = target / f"{chunk_index}.part"
        partial.write_bytes(encrypt_chunk(file_key, chunk_index, plaintext, self.segment_size))
        os.replace(partial, target / f"{chunk_index}.bin")

    def received_chunks(self, user_id: str, upload_id: str) -> list[int]:
        target = self._upload_dir(user_id, upload_id)
        if not target.is_dir():
            return []
        return sorted(int(p.stem) for p in target.glob("*.bin"))

    def finish_upload(self, user_id: str, upload_id: str) -> None:
        destination = self._file_dir(user_id, upload_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self._upload_dir(user_id, upload_id), destination)

    def discard_upload(self, user_id: str, upload_id: str) -> None:
        shutil.rmtree(self._upload_dir(user_id, upload_id), ignore_errors=True)

    def delete_file(self, user_id: str, node_id: str) -> None:
        shutil.rmtree(self._file_dir(user_id, node_id), ignore_errors=True)
        self._thumbnail(user_id, node_id).unlink(missing_ok=True)

    def read_range(self, user_id: str, node_id: str, file_key: bytes, start: int, end: int) -> Iterator[bytes]:
        """Yields plaintext for bytes start..end inclusive, decrypting only the segments that overlap it."""
        sealed_segment = self.segment_size + SEGMENT_OVERHEAD
        segments_per_chunk = self.chunk_size // self.segment_size
        position = start
        while position <= end:
            chunk_index, in_chunk = divmod(position, self.chunk_size)
            segment_index, in_segment = divmod(in_chunk, self.segment_size)
            with open(self._file_dir(user_id, node_id) / f"{chunk_index}.bin", "rb") as handle:
                while segment_index < segments_per_chunk and position <= end:
                    handle.seek(segment_index * sealed_segment)
                    sealed = handle.read(sealed_segment)
                    if not sealed:
                        return
                    plaintext = decrypt_segment(file_key, chunk_index, segment_index, sealed)
                    piece = plaintext[in_segment : in_segment + (end - position + 1)]
                    yield piece
                    position += len(piece)
                    segment_index += 1
                    in_segment = 0

    def write_thumbnail(self, user_id: str, node_id: str, cipher: UserCipher, jpeg: bytes) -> None:
        path = self._thumbnail(user_id, node_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(seal(cipher.thumbnail_key(node_id), jpeg, node_id.encode()))

    def read_thumbnail(self, user_id: str, node_id: str, cipher: UserCipher) -> bytes | None:
        path = self._thumbnail(user_id, node_id)
        if not path.is_file():
            return None
        return open_sealed(cipher.thumbnail_key(node_id), path.read_bytes(), node_id.encode())

    def delete_user(self, user_id: str) -> None:
        shutil.rmtree(self._user(user_id), ignore_errors=True)
