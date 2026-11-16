import pytest
from httpx import ASGITransport, AsyncClient

from app.blobstore import BlobStore
from app.cache import UnlockedKeys
from app.config import Settings, get_settings
from app.db import build_engine, build_sessionmaker, create_schema
from app.main import create_app
from app.sessions import SessionUser
from tests.fakeredis import FakeRedis

SERVICE_TOKEN = "test-service-token"
SERVICE_HEADERS = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
PASSWORD = "correct-horse-battery-staple"


class FakeSessions:
    def __init__(self) -> None:
        self.users = {
            "alice-session": SessionUser("user-alice", "alice", "alice-session"),
            "alice-second-session": SessionUser("user-alice", "alice", "alice-second-session"),
            "bob-session": SessionUser("user-bob", "bob", "bob-session"),
        }

    async def verify(self, token: str) -> SessionUser | None:
        return self.users.get(token)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        data_dir=str(tmp_path / "data"),
        service_token=SERVICE_TOKEN,
        key_pepper="test-pepper",
        key_cache_secret="test-cache-secret",
        default_quota_bytes=1000,
        chunk_size=64,
        segment_size=16,
        argon2_time_cost=1,
        argon2_memory_cost=8,
        argon2_parallelism=1,
    )


@pytest.fixture
async def app(settings, monkeypatch):
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: settings

    engine = build_engine(settings)
    application.state.engine = engine
    application.state.sessionmaker = build_sessionmaker(engine)
    application.state.keys = UnlockedKeys(settings, FakeRedis())
    application.state.sessions = FakeSessions()
    application.state.blobs = BlobStore(settings.data_dir, settings.chunk_size, settings.segment_size)
    await create_schema(engine)

    yield application

    await engine.dispose()
    get_settings.cache_clear()


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://storage-api") as c:
        yield c


def as_user(client, token="alice-session"):
    client.cookies.set("ck_sso", token)
    client.headers.update(SERVICE_HEADERS)
    return client


@pytest.fixture
async def alice(client):
    as_user(client)
    response = await client.post("/api/v1/vault/unlock", json={"password": PASSWORD})
    assert response.status_code == 201
    client.recovery_code = response.json()["recovery_code"]
    return client
