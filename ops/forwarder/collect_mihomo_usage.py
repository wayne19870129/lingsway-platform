"""Persistently attribute Mihomo connection counters to traffic buckets."""

import argparse
import json
import os
import signal
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
CREATE TABLE IF NOT EXISTS connection_maxima (
    connection_id TEXT PRIMARY KEY,
    side TEXT NOT NULL CHECK (side IN ('AIRPORT', 'RESIDENTIAL')),
    host TEXT NOT NULL,
    max_upload INTEGER NOT NULL CHECK (max_upload >= 0),
    max_download INTEGER NOT NULL CHECK (max_download >= 0),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_connection_maxima_last_seen
    ON connection_maxima(last_seen_at);
CREATE TABLE IF NOT EXISTS daily_usage (
    usage_day TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('AIRPORT', 'RESIDENTIAL')),
    upload_bytes INTEGER NOT NULL DEFAULT 0,
    download_bytes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (usage_day, side)
);
CREATE TABLE IF NOT EXISTS domain_usage (
    usage_day TEXT NOT NULL,
    host TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('AIRPORT', 'RESIDENTIAL')),
    upload_bytes INTEGER NOT NULL DEFAULT 0,
    download_bytes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (usage_day, host, side)
);
CREATE INDEX IF NOT EXISTS ix_domain_usage_host_side
    ON domain_usage(host, side);
CREATE TABLE IF NOT EXISTS collector_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    last_success_at TEXT,
    last_error_type TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO collector_state(singleton) VALUES (1);
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


def open_store(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def normalized_host(metadata: dict[str, Any]) -> str:
    value = str(metadata.get("host") or metadata.get("destinationIP") or "UNKNOWN")
    return value.rstrip(".").lower()[:253] or "UNKNOWN"


def classify_connection(item: dict[str, Any], residential_chain: str) -> str:
    chains = item.get("chains") or []
    return "RESIDENTIAL" if residential_chain in chains else "AIRPORT"


def add_usage(
    db: sqlite3.Connection,
    *,
    usage_day: str,
    side: str,
    host: str,
    upload_delta: int,
    download_delta: int,
) -> None:
    if upload_delta == 0 and download_delta == 0:
        return
    db.execute(
        """
        INSERT INTO daily_usage(usage_day, side, upload_bytes, download_bytes)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(usage_day, side) DO UPDATE SET
            upload_bytes = upload_bytes + excluded.upload_bytes,
            download_bytes = download_bytes + excluded.download_bytes
        """,
        (usage_day, side, upload_delta, download_delta),
    )
    db.execute(
        """
        INSERT INTO domain_usage(usage_day, host, side, upload_bytes, download_bytes)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(usage_day, host, side) DO UPDATE SET
            upload_bytes = upload_bytes + excluded.upload_bytes,
            download_bytes = download_bytes + excluded.download_bytes
        """,
        (usage_day, host, side, upload_delta, download_delta),
    )


def ingest_connections(
    db: sqlite3.Connection,
    connections: list[dict[str, Any]],
    *,
    observed_at: datetime,
    residential_chain: str,
) -> dict[str, int]:
    timestamp = observed_at.astimezone(UTC).isoformat()
    usage_day = observed_at.astimezone(UTC).date().isoformat()
    totals = {"connections": 0, "upload_delta": 0, "download_delta": 0}
    with db:
        for item in connections:
            connection_id = str(item.get("id") or "")
            if not connection_id:
                continue
            upload = max(0, int(item.get("upload") or 0))
            download = max(0, int(item.get("download") or 0))
            existing = db.execute(
                """SELECT side, host, max_upload, max_download
                   FROM connection_maxima WHERE connection_id = ?""",
                (connection_id,),
            ).fetchone()
            if existing is None:
                side = classify_connection(item, residential_chain)
                host = normalized_host(item.get("metadata") or {})
                upload_delta = upload
                download_delta = download
                db.execute(
                    """INSERT INTO connection_maxima(
                           connection_id, side, host, max_upload, max_download,
                           first_seen_at, last_seen_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (connection_id, side, host, upload, download, timestamp, timestamp),
                )
            else:
                side = str(existing["side"])
                host = str(existing["host"])
                upload_delta = max(0, upload - int(existing["max_upload"]))
                download_delta = max(0, download - int(existing["max_download"]))
                db.execute(
                    """UPDATE connection_maxima SET
                           max_upload = MAX(max_upload, ?),
                           max_download = MAX(max_download, ?),
                           last_seen_at = ?
                       WHERE connection_id = ?""",
                    (upload, download, timestamp, connection_id),
                )
            add_usage(
                db,
                usage_day=usage_day,
                side=side,
                host=host,
                upload_delta=upload_delta,
                download_delta=download_delta,
            )
            totals["connections"] += 1
            totals["upload_delta"] += upload_delta
            totals["download_delta"] += download_delta
        db.execute(
            """UPDATE collector_state SET last_success_at = ?, last_error_type = NULL,
               consecutive_errors = 0 WHERE singleton = 1""",
            (timestamp,),
        )
    return totals


def record_error(db: sqlite3.Connection, error_type: str) -> None:
    with db:
        db.execute(
            """UPDATE collector_state SET last_error_type = ?,
               consecutive_errors = consecutive_errors + 1 WHERE singleton = 1""",
            (error_type[:100],),
        )


def prune_connection_maxima(
    db: sqlite3.Connection, now: datetime, retention_days: int = 30
) -> None:
    cutoff = (now.astimezone(UTC) - timedelta(days=retention_days)).isoformat()
    with db:
        db.execute("DELETE FROM connection_maxima WHERE last_seen_at < ?", (cutoff,))


def build_report(db: sqlite3.Connection, generated_at: datetime | None = None) -> dict[str, Any]:
    generated_at = generated_at or utc_now()
    totals = {"AIRPORT": 0, "RESIDENTIAL": 0}
    for row in db.execute(
        """SELECT side, SUM(upload_bytes + download_bytes) AS bytes
           FROM daily_usage GROUP BY side"""
    ):
        totals[str(row["side"])] = int(row["bytes"] or 0)
    combined = totals["AIRPORT"] + totals["RESIDENTIAL"]
    daily_rows = list(
        db.execute(
            """SELECT usage_day, side, upload_bytes, download_bytes
               FROM daily_usage ORDER BY usage_day, side"""
        )
    )
    daily: dict[str, dict[str, int]] = {}
    for row in daily_rows:
        bucket = daily.setdefault(
            str(row["usage_day"]), {"airport_bytes": 0, "residential_bytes": 0}
        )
        bucket[f"{str(row['side']).lower()}_bytes"] = int(row["upload_bytes"]) + int(
            row["download_bytes"]
        )
    if daily:
        first = date.fromisoformat(min(daily))
        last = date.fromisoformat(max(daily))
        observed_calendar_days = (last - first).days + 1
    else:
        observed_calendar_days = 0
    top_domains = [
        {
            "host": str(row["host"]),
            "side": str(row["side"]),
            "bytes": int(row["bytes"] or 0),
        }
        for row in db.execute(
            """SELECT host, side, SUM(upload_bytes + download_bytes) AS bytes
               FROM domain_usage GROUP BY host, side ORDER BY bytes DESC LIMIT 20"""
        )
    ]
    state = db.execute("SELECT * FROM collector_state WHERE singleton = 1").fetchone()
    return {
        "generated_at": generated_at.astimezone(UTC).isoformat(),
        "last_success_at": state["last_success_at"] if state else None,
        "last_error_type": state["last_error_type"] if state else None,
        "consecutive_errors": int(state["consecutive_errors"] if state else 0),
        "airport_bytes": totals["AIRPORT"],
        "residential_bytes": totals["RESIDENTIAL"],
        "residential_ratio": totals["RESIDENTIAL"] / combined if combined else None,
        "observed_calendar_days": observed_calendar_days,
        "average_daily_residential_bytes": (
            totals["RESIDENTIAL"] // observed_calendar_days
            if observed_calendar_days
            else None
        ),
        "daily": [{"date": key, **daily[key]} for key in sorted(daily)],
        "top_domains": top_domains,
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def fetch_connections(client: httpx.Client, api_url: str, secret: str) -> list[dict[str, Any]]:
    response = client.get(
        f"{api_url.rstrip('/')}/connections",
        headers={"Authorization": f"Bearer {secret}"},
    )
    response.raise_for_status()
    document = response.json()
    return list(document.get("connections") or [])


def run_loop(
    db: sqlite3.Connection,
    *,
    api_url: str,
    secret: str,
    residential_chain: str,
    report_path: Path,
    interval: float,
) -> None:
    running = True

    def stop(*_: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last_prune: date | None = None
    with httpx.Client(timeout=10.0) as client:
        while running:
            started = time.monotonic()
            now = utc_now()
            try:
                connections = fetch_connections(client, api_url, secret)
                ingest_connections(
                    db,
                    connections,
                    observed_at=now,
                    residential_chain=residential_chain,
                )
                if last_prune != now.date():
                    prune_connection_maxima(db, now)
                    last_prune = now.date()
                write_report(report_path, build_report(db, now))
            except Exception as exc:
                record_error(db, type(exc).__name__)
                write_report(report_path, build_report(db, now))
            remaining = interval - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    store_path = Path(
        os.environ.get("MIHOMO_ATTRIBUTION_DB", "/app/data/attribution/usage.sqlite3")
    )
    report_path = Path(
        os.environ.get("MIHOMO_ATTRIBUTION_REPORT", "/app/data/attribution/report.json")
    )
    db = open_store(store_path)
    if args.report:
        print(json.dumps(build_report(db), ensure_ascii=False, indent=2, sort_keys=True))
        return
    api_url = os.environ.get("MIHOMO_API_URL", "http://mihomo:9090")
    secret = os.environ["MIHOMO_API_SECRET"]
    residential_chain = os.environ.get("RESIDENTIAL_CHAIN_NAME", "EGRESS_US_01")
    if args.once:
        with httpx.Client(timeout=10.0) as client:
            connections = fetch_connections(client, api_url, secret)
        ingest_connections(
            db,
            connections,
            observed_at=utc_now(),
            residential_chain=residential_chain,
        )
        write_report(report_path, build_report(db))
        return
    run_loop(
        db,
        api_url=api_url,
        secret=secret,
        residential_chain=residential_chain,
        report_path=report_path,
        interval=float(os.environ.get("MIHOMO_ATTRIBUTION_INTERVAL_SECONDS", "2")),
    )


if __name__ == "__main__":
    main()
