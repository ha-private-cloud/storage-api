import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app import library
from app.blobstore import BlobStore
from app.cache import UnlockedKeys, build_client
from app.config import Settings, get_settings
from app.db import build_engine, build_sessionmaker, create_schema
from app.routers import admin, files, health, vault
from app.sessions import SessionVerifier

logger = logging.getLogger("storage-api")


async def purge_loop(app: FastAPI, settings: Settings) -> None:
    while True:
        try:
            async with app.state.sessionmaker() as db:
                removed = await library.purge_expired(db, app.state.blobs, settings.trash_retention_days)
            if removed:
                logger.info("purged %d expired trash items", removed)
        except Exception:
            logger.exception("trash purge failed")
        await asyncio.sleep(settings.trash_purge_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.engine = build_engine(settings)
    app.state.sessionmaker = build_sessionmaker(app.state.engine)
    app.state.keys = UnlockedKeys(settings, build_client(settings))
    app.state.http = httpx.AsyncClient()
    app.state.sessions = SessionVerifier(settings, app.state.http)
    app.state.blobs = BlobStore(settings.data_dir, settings.chunk_size, settings.segment_size)

    await create_schema(app.state.engine)
    purger = asyncio.create_task(purge_loop(app, settings))

    yield

    purger.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await purger
    await app.state.http.aclose()
    await app.state.engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title="ClusterKeep storage-api",
        version="0.1.0",
        description="Per-user encrypted file storage for ClusterKeep, reachable only inside the cluster.",
        lifespan=lifespan,
        docs_url="/docs" if settings.environment != "prd" else None,
        redoc_url=None,
    )
    application.include_router(health.router)
    application.include_router(vault.router)
    application.include_router(files.router)
    application.include_router(admin.router)
    return application


app = create_app()
