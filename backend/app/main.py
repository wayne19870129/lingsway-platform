"""Application entry point for the backend API."""

from fastapi import FastAPI

from backend.app.api.health import router as health_router
from backend.app.api.public import router as public_router

app = FastAPI(title="Lingsway Platform")

app.include_router(health_router, prefix="/api/v1")
app.include_router(public_router, prefix="/api/v1")


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
