# asc-testflight-feedback

**TestFlight beta feedback for every app on your App Store Connect team, pulled
into Postgres and served over one small read API.**

Apple shows beta feedback in App Store Connect and nowhere else. There is no
export, and the API resource is easy to miss. If you want tester screenshots and
crash reports in your own dashboard — or in front of an agent that triages
them — you have to go and get them. This goes and gets them.

```sh
cp .env.example .env     # three App Store Connect values + a Postgres password
docker compose up -d
curl -s localhost:8000/health
```

---

## What it is not

- **Not App Store reviews.** Public star ratings on a released app are a
  different resource entirely.
- **Not in-app feedback.** Whatever SDK collects that is a separate pipeline.
- **Not tenant-aware.** This service knows Apple app ids and bundle ids.
  Mapping those to *your* customers is the consumer's job — deliberately, so a
  second consumer does not inherit the first one's tenant model.

## Endpoints

| | |
|---|---|
| `GET /health` | `ok`, `degraded` (no key yet — still 200), or `unhealthy` (503, Postgres unreachable) |
| `GET /v1/apps` | every app seen, with feedback counts and newest submission |
| `GET /v1/feedback` | `?app_id=` `?bundle_id=` `?kind=screenshot\|crash` `?since=` `?limit=` |
| `POST /v1/sync` | pull now instead of waiting for the poller |

With no credentials yet, it tells you exactly that and stays up:

```console
$ curl -s localhost:8000/health
{"status":"degraded",
 "reason":"App Store Connect credentials not configured",
 "missing":["ASC_ISSUER_ID","ASC_KEY_ID",
            "ASC_PRIVATE_KEY_PATH (no readable .p8 at /run/secrets/asc_key.p8)"],
 "database":"ok"}

$ curl -s -X POST localhost:8000/v1/sync
{"detail":{"error":"not_configured",
           "missing":[...],
           "fix":"Add the App Store Connect key to the service .env and restart."}}
```

`degraded` is a 200 and `/v1/sync` is a 503, because *missing credentials is a
state you chose* and an unreachable database is not. An unconfigured service
that reports healthy is the failure this shape exists to refuse.

---

## The credential — read this part

App Store Connect auth is a **three-part** credential: an issuer id, a key id,
and a `.p8` private key. Two of three fails at 401.

Generate it at **App Store Connect → Users and Access → Integrations → App
Store Connect API**. The `.p8` downloads exactly **once** and cannot be
re-downloaded.

> **Mint a NEW key for this service. Do not reuse the one that uploads builds.**
>
> This is worth a sentence because it is easy to get wrong and expensive to
> discover later. An ASC key that can sign and upload can **ship a release**. A
> read-only feedback poller that borrows that key is a service whose blast
> radius is "push a build to your users" — and it will sit in a `.env` on a box
> for a year while everyone assumes it only reads screenshots. Give this the
> narrowest role that can read beta feedback, and keep the two keys separate.

The key is mounted **read-only**, as a **file** not a directory, so nothing else
in your key directory is exposed to the container.

> **Trap worth knowing:** a single-file bind mount is pinned to the file's
> inode. Replacing the key on the host (rather than editing in place) leaves the
> container reading the **old** key until it is restarted — `docker restart`,
> not a config reload.

The container runs as `RUN_AS_UID:RUN_AS_GID` from your `.env` so it can read a
key that is mode 600. Match the container to the key; do not widen the key to
suit the container.

---

## Why polling, not Apple's webhook

App Store Connect can push a webhook on new feedback, which would be faster. It
also requires a **public endpoint accepting anonymous POSTs**, which means
cutting a hole in whatever currently protects your hostnames. That is a security
decision worth making on its own merits, later, once the data path is proven.
Polling needs no public exposure at all.

Default interval is 15 minutes (`POLL_SECONDS`). Beta feedback is not
real-time-critical and Apple rate limits.

## Why raw payloads are stored alongside extracted fields

Apple adds attributes to these resources without notice, and the two feedback
resources — screenshot and crash — do not share a schema. Extracting a common
shape keeps the read API stable for consumers; keeping the raw record means a
field nobody anticipated is still recoverable **without re-polling Apple**,
which is rate limited.

---

## Deployment notes

The bundled `docker-compose.yml` is self-contained — it brings its own Postgres.
If you already run one, delete the `postgres` service and point `PGHOST` at
yours.

The API is bound to **loopback** (`127.0.0.1:8000`). This service holds an Apple
credential and has **no authentication of its own**: it is meant to sit behind
whatever already fronts your other services. Change the bind address only once
something is authenticating in front of it.

Base image and Postgres are both **digest-pinned**, and Python dependencies are
exactly pinned, so a rebuild a year from now is the same stack this was verified
against.

---

## Develop

```sh
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Requires a reachable Postgres (`PGUSER`, `PGPASSWORD`, `PGHOST`, `PGDATABASE`);
migrations run at startup.

## License

MIT © David Hook
