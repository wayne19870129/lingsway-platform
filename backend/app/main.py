"""Application entry point for the backend API."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from backend.app.api.admin import router as admin_router
from backend.app.api.health import router as health_router
from backend.app.api.public import router as public_router
from backend.app.api.subscription import router as subscription_router
from backend.app.api.subscription import subscription_feed_router
from backend.app.core.config import get_settings
from backend.app.infra.gateway_reconciliation import reconcile_gateway_job
from backend.app.providers.registry import ProviderRegistry, build_registry


async def _gateway_reconciliation_loop(registry: ProviderRegistry, stop: asyncio.Event) -> None:
    """Recover durable gateway intents without owning another provider registry."""
    while not stop.is_set():
        with suppress(Exception):
            await asyncio.to_thread(reconcile_gateway_job, registry)
        try:
            await asyncio.wait_for(stop.wait(), timeout=5.0)
        except TimeoutError:
            continue


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """TASK-T16 Phase 2B8: build exactly one process-lifetime
    ``ProviderRegistry`` on startup, and close it deterministically on
    shutdown -- the single ownership boundary every HTTP request reuses
    (see ``backend.app.dependencies.get_provider_registry``), instead of
    each request/route constructing and discarding its own registry
    (and, for a future resource-owning provider, an unclosed
    ``httpx.Client`` per request).
    """
    settings = get_settings()
    registry = build_registry(settings)
    app.state.provider_registry = registry
    stop = asyncio.Event()
    recovery_task = (
        asyncio.create_task(_gateway_reconciliation_loop(registry, stop))
        if settings.gateway_provider == "xray_file"
        else None
    )
    try:
        yield
    finally:
        stop.set()
        if recovery_task is not None:
            recovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await recovery_task
        registry.close()


app = FastAPI(title="Lingsway Platform", lifespan=lifespan)

app.include_router(health_router, prefix="/api/v1")
app.include_router(public_router, prefix="/api/v1")
app.include_router(subscription_router, prefix="/api/v1")
app.include_router(subscription_feed_router)
app.include_router(admin_router, prefix="/api/v1")


@app.get("/health")
def health() -> dict[str, str]:
    """Return the liveness status used by deployment verification."""
    return {"status": "ok"}


def application_name() -> str:
    """Return the stable service name used by skeleton checks."""
    return "lingsway-platform"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
