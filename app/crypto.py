import base64
import hashlib
import hmac
import secrets

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import Settings

KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
SALT_BYTES = 16
SEGMENT_OVERHEAD = NONCE_BYTES + TAG_BYTES
RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
RECOVERY_GROUPS = 6
RECOVERY_GROUP_LENGTH = 5


class DecryptionFailed(Exception):
    pass


def new_key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


def new_salt() -> bytes:
    return secrets.token_bytes(SALT_BYTES)


def new_recovery_code() -> str:
    groups = (
        "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_GROUP_LENGTH))
        for _ in range(RECOVERY_GROUPS)
    )
    return "-".join(groups)


def normalize_recovery_code(code: str) -> str:
    cleaned = "".join(ch for ch in code.upper() if ch.isalnum())
    return "-".join(
        cleaned[i : i + RECOVERY_GROUP_LENGTH] for i in range(0, len(cleaned), RECOVERY_GROUP_LENGTH)
    )


def derive_wrapping_key(secret: str, salt: bytes, settings: Settings) -> bytes:
    material = secret.encode("utf-8")
    if settings.key_pepper:
        material = hmac.new(settings.key_pepper.encode("utf-8"), material, hashlib.sha512).digest()
    return hash_secret_raw(
        secret=material,
        salt=salt,
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_cost,
        parallelism=settings.argon2_parallelism,
        hash_len=KEY_BYTES,
        type=Type.ID,
    )


def seal(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = secrets.token_bytes(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def open_sealed(key: bytes, sealed: bytes, aad: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(sealed[:NONCE_BYTES], sealed[NONCE_BYTES:], aad)
    except (InvalidTag, ValueError) as exc:
        raise DecryptionFailed() from exc


def _subkey(key: bytes, purpose: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=purpose.encode("utf-8")).derive(key)


class UserCipher:
    """Everything derived from one user's data key: names, file contents and thumbnails each get their own subkey."""

    def __init__(self, data_key: bytes) -> None:
        self._data_key = data_key
        self._name_key = _subkey(data_key, "names")

    def encrypt_name(self, node_id: str, name: str) -> str:
        return base64.urlsafe_b64encode(seal(self._name_key, name.encode("utf-8"), node_id.encode())).decode()

    def decrypt_name(self, node_id: str, encrypted: str) -> str:
        return open_sealed(self._name_key, base64.urlsafe_b64decode(encrypted), node_id.encode()).decode("utf-8")

    def file_key(self, node_id: str) -> bytes:
        return _subkey(self._data_key, f"file:{node_id}")

    def thumbnail_key(self, node_id: str) -> bytes:
        return _subkey(self._data_key, f"thumbnail:{node_id}")


def segment_aad(chunk_index: int, segment_index: int) -> bytes:
    return f"{chunk_index}:{segment_index}".encode()


def encrypt_chunk(file_key: bytes, chunk_index: int, plaintext: bytes, segment_size: int) -> bytes:
    out = bytearray()
    for segment_index, start in enumerate(range(0, len(plaintext), segment_size)):
        out += seal(file_key, plaintext[start : start + segment_size], segment_aad(chunk_index, segment_index))
    return bytes(out)


def decrypt_segment(file_key: bytes, chunk_index: int, segment_index: int, sealed: bytes) -> bytes:
    return open_sealed(file_key, sealed, segment_aad(chunk_index, segment_index))


class KeyCacheSealer:
    """Unlocked data keys sit in Redis only in sealed form, so a Redis dump alone opens nothing."""

    def __init__(self, secret: str) -> None:
        self._key = _subkey(secret.encode("utf-8"), "key-cache")

    def seal(self, user_id: str, data_key: bytes) -> bytes:
        return seal(self._key, data_key, user_id.encode())

    def open(self, user_id: str, sealed: bytes) -> bytes:
        return open_sealed(self._key, sealed, user_id.encode())
