"""asc-testflight-feedback — one ingestion, many consumers.

Pulls TestFlight beta feedback for EVERY app the App Store Connect key can see
and serves it over a small read API. One consumer renders it in a customer view; anything else that can reach the
service reads the same endpoint.

WHY POLLING, NOT APPLE'S WEBHOOK
App Store Connect can push a webhook on new feedback, which would be faster. It
also requires a public endpoint that accepts anonymous POSTs, which means cutting
a hole in whatever protects the rest of your hostnames. That is a security decision worth making on its own, later, once the
data path is proven. Polling needs no public exposure at all.

WHAT HAPPENS WITH NO CREDENTIALS
The service starts, /health reports `degraded` and says exactly what is missing,
and /v1/sync returns 503 with the same message. It does not crash-loop and it does
not report healthy. An unconfigured service that looks healthy is a trap worth refusing to build.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import store
from .asc import AppStoreConnect, ASCError, Credentials, NotConfigured, normalise

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("asc-testflight-feedback")

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "900"))


def _credentials() -> Credentials:
    key_path = os.environ.get("ASC_PRIVATE_KEY_PATH", "")
    private_key = ""
    if key_path and Path(key_path).is_file():
        try:
            private_key = Path(key_path).read_text()
        except OSError as e:
            # Unreadable is the same as absent as far as callers are concerned.
            # It must NOT propagate: a permissions problem on the key should show
            # up as DEGRADED with a readable reason, not as a 500 on /health.
            # Found 2026-08-28 - the placeholder key was mode 600 owned by the
            # host user while the container ran as a different uid.
            log.warning("cannot read %s: %s", key_path, e)
    return Credentials(
        issuer_id=os.environ.get("ASC_ISSUER_ID", ""),
        key_id=os.environ.get("ASC_KEY_ID", ""),
        private_key=private_key,
    )


def _missing() -> list[str]:
    creds = _credentials()
    missing = []
    if not creds.issuer_id:
        missing.append("ASC_ISSUER_ID")
    if not creds.key_id:
        missing.append("ASC_KEY_ID")
    if not creds.private_key:
        path = os.environ.get("ASC_PRIVATE_KEY_PATH", "(unset)")
        missing.append(f"ASC_PRIVATE_KEY_PATH (no readable .p8 at {path})")
    return missing


def sync_once() -> dict:
    """One full pass. Returns a summary; raises ASCError on a real failure."""
    client = AppStoreConnect(_credentials())
    apps = client.apps()
    store.upsert_apps(apps)

    total = 0
    for app in apps:
        app_id = app["asc_app_id"]
        for kind, fetch in (
            ("screenshot", client.screenshot_feedback),
            ("crash", client.crash_feedback),
        ):
            batch = [normalise(r, kind, app_id, inc) for r, inc in fetch(app_id)]
            total += store.upsert_feedback(batch)
            log.info("%s: %d %s submissions", app.get("bundle_id") or app_id, len(batch), kind)

    return {"apps": len(apps), "rows": total}


async def _poller():
    """Background loop. Never lets one failure kill the loop — a revoked key
    should degrade the service, not stop it retrying after the key is replaced."""
    while True:
        if _missing():
            log.warning("skipping sync: %s", ", ".join(_missing()))
        else:
            try:
                result = await asyncio.to_thread(sync_once)
                store.record_run(True, result["apps"], result["rows"], None)
                log.info("sync ok: %s", result)
            except ASCError as e:
                store.record_run(False, 0, 0, str(e))
                log.error("sync failed: %s", e)
            except Exception as e:  # noqa: BLE001 - the loop must survive anything
                store.record_run(False, 0, 0, repr(e))
                log.exception("sync raised")
        await asyncio.sleep(POLL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.migrate()
    task = asyncio.create_task(_poller())
    yield
    task.cancel()


app = FastAPI(title="asc-testflight-feedback", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health():
    missing = _missing()
    try:
        with store.connect() as conn:
            conn.execute("SELECT 1")
    except Exception as e:  # noqa: BLE001
        return JSONResponse(
            {"status": "unhealthy", "database": str(e)}, status_code=503
        )
    if missing:
        # 200, not 503: the service is up and correct, it simply has no key yet.
        # A red healthcheck here would page somebody for a state we chose.
        return {
            "status": "degraded",
            "reason": "App Store Connect credentials not configured",
            "missing": missing,
            "database": "ok",
        }
    return {"status": "ok", "database": "ok", "last_run": store.last_run()}


@app.get("/v1/apps")
def list_apps():
    """Every app seen, with how much feedback each has. The consumer maps these
    to its own tenants; this service deliberately knows nothing about tenants."""
    return {"apps": store.apps()}


@app.get("/v1/feedback")
def list_feedback(
    app_id: str | None = Query(None, description="App Store Connect app id"),
    bundle_id: str | None = Query(None, description="e.g. com.lbdavid98.eigencards"),
    kind: str | None = Query(None, pattern="^(screenshot|crash)$"),
    since: str | None = Query(None, description="ISO-8601; created_date >= this"),
    limit: int = Query(100, ge=1, le=500),
):
    return {
        "feedback": store.feedback(
            asc_app_id=app_id, bundle_id=bundle_id, kind=kind, since=since, limit=limit
        )
    }


@app.post("/v1/sync")
def trigger_sync():
    """Manual pull, for when you do not want to wait for the poller."""
    if _missing():
        raise HTTPException(
            status_code=503,
            detail={
                "error": "not_configured",
                "missing": _missing(),
                "fix": "Add the App Store Connect key to the service .env and restart.",
            },
        )
    try:
        result = sync_once()
    except NotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ASCError as e:
        store.record_run(False, 0, 0, str(e))
        raise HTTPException(status_code=502, detail=str(e)) from e
    store.record_run(True, result["apps"], result["rows"], None)
    return result
