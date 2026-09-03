"""Application entry point for the backend API."""

from fastapi import FastAPI

app = FastAPI(title="Lingsway Platform")


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
