from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STORAGE_API_", env_file=".env", extra="ignore")

    environment: str = "dev"
    database_url: str = "postgresql+asyncpg://storage_api:storage_api@postgres:5432/storage_api"
    redis_url: str = "redis://redis:6379/0"
    redis_namespace: str = "storageapi"
    data_dir: str = "/data"

    auth_api_internal_url: str = "http://auth-api.auth-dev-pub.svc.cluster.local"
    session_cookie_name: str = "ck_sso"
    session_check_seconds: int = 30

    service_token: str = ""
    key_pepper: str = ""
    key_cache_secret: str = ""
    unlocked_key_seconds: int = 60 * 60 * 24 * 7

    argon2_time_cost: int = 3
    argon2_memory_cost: int = 64 * 1024
    argon2_parallelism: int = 4

    default_quota_bytes: int = 10 * 1024**3
    chunk_size: int = 8 * 1024 * 1024
    segment_size: int = 1024 * 1024
    trash_retention_days: int = 30
    trash_purge_interval_seconds: int = 60 * 60
    thumbnail_max_source_bytes: int = 50 * 1024 * 1024
    video_poster_read_bytes: int = 64 * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
