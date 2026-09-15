"""Postgres store for TestFlight feedback.

CC-P-005: records go in Postgres, not a JSON file. This service holds the only
copy that consumers read, so the schema is deliberately consumer-facing rather
than a mirror of Apple's wire format.

Idempotent by submission id — a poll that overlaps the previous one re-upserts
the same rows rather than duplicating them, which is what makes the poller safe
to run on a short interval and safe to re-run by hand.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS testflight_app (
    asc_app_id   text PRIMARY KEY,
    bundle_id    text,
    name         text,
    sku          text,
    first_seen   timestamptz NOT NULL DEFAULT now(),
    last_synced  timestamptz
);

CREATE TABLE IF NOT EXISTS testflight_feedback (
    submission_id text PRIMARY KEY,
    kind          text NOT NULL CHECK (kind IN ('screenshot', 'crash')),
    asc_app_id    text NOT NULL REFERENCES testflight_app(asc_app_id),
    created_date  timestamptz,
    comment       text,
    app_version   text,
    build_number  text,
    device_model  text,
    os_version    text,
    locale        text,
    tester_id     text,
    raw           jsonb NOT NULL,
    ingested_at   timestamptz NOT NULL DEFAULT now()
);

-- The read path is always "newest feedback, optionally for one app".
CREATE INDEX IF NOT EXISTS testflight_feedback_app_created_idx
    ON testflight_feedback (asc_app_id, created_date DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS testflight_feedback_created_idx
    ON testflight_feedback (created_date DESC NULLS LAST);

-- Added 2026-08-29 once Apple's real payload was known. ADD COLUMN IF NOT EXISTS
-- keeps this file the single description of the schema without a migration tool.
ALTER TABLE testflight_feedback ADD COLUMN IF NOT EXISTS tester_name  text;
ALTER TABLE testflight_feedback ADD COLUMN IF NOT EXISTS tester_email text;
ALTER TABLE testflight_feedback ADD COLUMN IF NOT EXISTS screenshots  jsonb;

CREATE TABLE IF NOT EXISTS testflight_sync_run (
    id          bigserial PRIMARY KEY,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    ok          boolean,
    apps_seen   integer NOT NULL DEFAULT 0,
    rows_upsert integer NOT NULL DEFAULT 0,
    error       text
);
"""


def dsn() -> str:
    """Built from parts so the password never has to be URL-escaped by hand."""
    return (
        f"host={os.environ.get('PGHOST', 'postgres')} "
        f"port={os.environ.get('PGPORT', '5432')} "
        f"dbname={os.environ.get('PGDATABASE', 'app')} "
        f"user={os.environ['PGUSER']} "
        f"password={os.environ['PGPASSWORD']}"
    )


@contextmanager
def connect():
    with psycopg.connect(dsn(), row_factory=dict_row) as conn:
        yield conn


def migrate() -> None:
    with connect() as conn:
        conn.execute(SCHEMA)
        conn.commit()
    log.info("schema ready")


def upsert_apps(apps: Iterable[dict]) -> int:
    rows = list(apps)
    if not rows:
        return 0
    with connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO testflight_app (asc_app_id, bundle_id, name, sku, last_synced)
                VALUES (%(asc_app_id)s, %(bundle_id)s, %(name)s, %(sku)s, now())
                ON CONFLICT (asc_app_id) DO UPDATE SET
                    bundle_id   = EXCLUDED.bundle_id,
                    name        = EXCLUDED.name,
                    sku         = EXCLUDED.sku,
                    last_synced = now()
                """,
                rows,
            )
        conn.commit()
    return len(rows)


def upsert_feedback(records: Iterable[dict]) -> int:
    rows = [
        dict(r, raw=json.dumps(r["raw"]),
             screenshots=json.dumps(r["screenshots"]) if r.get("screenshots") else None)
        for r in records
    ]
    if not rows:
        return 0
    with connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO testflight_feedback (
                    submission_id, kind, asc_app_id, created_date, comment,
                    app_version, build_number, device_model, os_version, locale,
                    tester_id, tester_name, tester_email, screenshots, raw
                ) VALUES (
                    %(submission_id)s, %(kind)s, %(asc_app_id)s, %(created_date)s, %(comment)s,
                    %(app_version)s, %(build_number)s, %(device_model)s, %(os_version)s, %(locale)s,
                    %(tester_id)s, %(tester_name)s, %(tester_email)s, %(screenshots)s, %(raw)s
                )
                ON CONFLICT (submission_id) DO UPDATE SET
                    comment      = EXCLUDED.comment,
                    build_number = EXCLUDED.build_number,
                    tester_name  = EXCLUDED.tester_name,
                    tester_email = EXCLUDED.tester_email,
                    screenshots  = EXCLUDED.screenshots,
                    raw          = EXCLUDED.raw
                """,
                rows,
            )
        conn.commit()
    return len(rows)


def newest_created(asc_app_id: str) -> str | None:
    """Cursor for incremental polling."""
    with connect() as conn:
        row = conn.execute(
            "SELECT max(created_date) AS m FROM testflight_feedback WHERE asc_app_id = %s",
            (asc_app_id,),
        ).fetchone()
    return row["m"].isoformat() if row and row["m"] else None


def feedback(
    asc_app_id: str | None = None,
    bundle_id: str | None = None,
    kind: str | None = None,
    since: str | None = None,
    limit: int = 100,
) -> list[dict]:
    clauses, params = [], {}
    if asc_app_id:
        clauses.append("f.asc_app_id = %(asc_app_id)s")
        params["asc_app_id"] = asc_app_id
    if bundle_id:
        clauses.append("a.bundle_id = %(bundle_id)s")
        params["bundle_id"] = bundle_id
    if kind:
        clauses.append("f.kind = %(kind)s")
        params["kind"] = kind
    if since:
        clauses.append("f.created_date >= %(since)s")
        params["since"] = since
    params["limit"] = max(1, min(limit, 500))

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT f.submission_id, f.kind, f.asc_app_id, a.bundle_id, a.name AS app_name,
               f.created_date, f.comment, f.app_version, f.build_number,
               f.device_model, f.os_version, f.locale, f.tester_id,
               f.tester_name, f.tester_email, f.screenshots, f.ingested_at
        FROM testflight_feedback f
        JOIN testflight_app a USING (asc_app_id)
        {where}
        ORDER BY f.created_date DESC NULLS LAST
        LIMIT %(limit)s
    """
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def apps() -> list[dict]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT a.asc_app_id, a.bundle_id, a.name, a.last_synced,
                   count(f.submission_id) AS feedback_count,
                   max(f.created_date)    AS newest_feedback
            FROM testflight_app a
            LEFT JOIN testflight_feedback f USING (asc_app_id)
            GROUP BY a.asc_app_id, a.bundle_id, a.name, a.last_synced
            ORDER BY a.name NULLS LAST
            """
        ).fetchall()


def record_run(ok: bool, apps_seen: int, rows: int, error: str | None) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO testflight_sync_run (finished_at, ok, apps_seen, rows_upsert, error)
            VALUES (now(), %s, %s, %s, %s)
            """,
            (ok, apps_seen, rows, error),
        )
        conn.commit()


def last_run() -> dict[str, Any] | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM testflight_sync_run ORDER BY id DESC LIMIT 1"
        ).fetchone()
