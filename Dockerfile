# asc-testflight-feedback
#
# Non-root, digest-pinned base, no build toolchain in the final layer.

FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217 AS build

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217

# A fixed uid so the mounted .p8 can be chowned to something predictable on the
# host without granting it to anyone else.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin tfeed

COPY --from=build /install /usr/local
WORKDIR /srv
COPY app ./app

USER tfeed
EXPOSE 8000

# The healthcheck hits /health, which reports `degraded` (200) without a key and
# `unhealthy` (503) only when Postgres is unreachable. Missing credentials are a
# state we chose; an unreachable database is not.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=8).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
