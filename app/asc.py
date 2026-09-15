"""App Store Connect API client — TestFlight beta feedback.

Auth is a short-lived ES256 JWT, signed with a .p8 private key issued in App Store
Connect under Users and Access -> Integrations. Apple caps token lifetime at 20
minutes; we mint for 15 and re-mint on demand rather than caching across a poll.

WHY RAW PAYLOADS ARE STORED ALONGSIDE EXTRACTED FIELDS
Apple adds attributes to these resources without notice, and the two feedback
resources do not share a schema. Extracting a common shape makes the read API
stable for consumers; keeping the raw record means a field we did not anticipate
is still recoverable without re-polling Apple, which is rate limited.
"""

from __future__ import annotations

import time
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator

import jwt  # PyJWT, with the cryptography extra for ES256

log = logging.getLogger(__name__)

BASE = "https://api.appstoreconnect.apple.com"
AUDIENCE = "appstoreconnect-v1"
TOKEN_TTL = 15 * 60


class ASCError(RuntimeError):
    """Anything that came back from Apple that we cannot act on."""


class NotConfigured(ASCError):
    """No credentials. Distinct from a failure, because it is not one."""


@dataclass(frozen=True)
class Credentials:
    issuer_id: str
    key_id: str
    private_key: str

    @property
    def configured(self) -> bool:
        return bool(self.issuer_id and self.key_id and self.private_key)


class AppStoreConnect:
    def __init__(self, creds: Credentials, timeout: int = 30):
        self._creds = creds
        self._timeout = timeout

    # -- auth ------------------------------------------------------------

    def _token(self) -> str:
        c = self._creds
        if not c.configured:
            raise NotConfigured(
                "App Store Connect credentials are absent. Set ASC_ISSUER_ID, "
                "ASC_KEY_ID and ASC_PRIVATE_KEY_PATH."
            )
        now = int(time.time())
        return jwt.encode(
            {"iss": c.issuer_id, "iat": now, "exp": now + TOKEN_TTL, "aud": AUDIENCE},
            c.private_key,
            algorithm="ES256",
            headers={"kid": c.key_id, "typ": "JWT"},
        )

    # -- transport -------------------------------------------------------

    def _get(self, url: str) -> dict[str, Any]:
        if not url.startswith(BASE):
            # Apple returns absolute pagination links. Refuse anything that is not
            # Apple's own host rather than following a redirect off-domain with a
            # bearer token attached.
            raise ASCError(f"refusing to follow a link outside {BASE}: {url}")

        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:600]
            if e.code == 401:
                raise ASCError(
                    "App Store Connect rejected the key (401). Check the key has not "
                    f"been revoked and that the issuer id matches. {body}"
                ) from e
            if e.code == 403:
                raise ASCError(
                    "App Store Connect refused the request (403). The key's role is "
                    f"probably too narrow to read beta feedback. {body}"
                ) from e
            if e.code == 429:
                raise ASCError(f"rate limited by App Store Connect (429). {body}") from e
            raise ASCError(f"App Store Connect returned {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise ASCError(f"could not reach App Store Connect: {e.reason}") from e

    def _paged(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
        """Yield every record across Apple's cursor pagination.

        Bounded by MAX_PAGES so a malformed `next` link cannot spin forever.
        """
        url = f"{BASE}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        pages = 0
        while url and pages < 200:
            payload = self._get(url)
            for record in payload.get("data", []):
                yield record
            url = (payload.get("links") or {}).get("next") or ""
            pages += 1
        if pages >= 200:
            log.warning("stopped paginating %s at %d pages", path, pages)

    # -- resources -------------------------------------------------------

    def apps(self) -> list[dict]:
        """Every app the key can see. This is what makes 'all apps' automatic —
        a new app on the team is picked up without a config change."""
        return [
            {
                "asc_app_id": r["id"],
                "bundle_id": (r.get("attributes") or {}).get("bundleId"),
                "name": (r.get("attributes") or {}).get("name"),
                "sku": (r.get("attributes") or {}).get("sku"),
            }
            for r in self._paged("/v1/apps", {"limit": 200})
        ]

    def _paged_included(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Iterator[tuple[dict, dict]]:
        """Like _paged, but also hands back that page's `included` resources,
        keyed by (type, id).

        Apple returns related resources in a sibling `included` array rather than
        inline, and omits the `relationships` block altogether unless `include` is
        requested. The index is per-page because ids are only guaranteed unique
        within the document that carried them.
        """
        url = f"{BASE}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        pages = 0
        while url and pages < 200:
            payload = self._get(url)
            included = {
                (i.get("type"), i.get("id")): i for i in payload.get("included", [])
            }
            for record in payload.get("data", []):
                yield record, included
            url = (payload.get("links") or {}).get("next") or ""
            pages += 1
        if pages >= 200:
            log.warning("stopped paginating %s at %d pages", path, pages)

    def screenshot_feedback(self, asc_app_id: str) -> Iterator[tuple[dict, dict]]:
        yield from self._paged_included(
            f"/v1/apps/{asc_app_id}/betaFeedbackScreenshotSubmissions",
            {"limit": 200, "sort": "-createdDate", "include": "build,tester"},
        )

    def crash_feedback(self, asc_app_id: str) -> Iterator[tuple[dict, dict]]:
        yield from self._paged_included(
            f"/v1/apps/{asc_app_id}/betaFeedbackCrashSubmissions",
            {"limit": 200, "sort": "-createdDate", "include": "build,tester"},
        )


def normalise(record: dict, kind: str, asc_app_id: str, included: dict) -> dict:
    """Flatten one Apple record into the shape consumers read.

    Defensive on purpose: every field is optional, because the two resources do
    not share a schema and Apple extends both. The raw record is kept whole.
    """
    attrs = record.get("attributes") or {}
    rels = record.get("relationships") or {}

    def related(name: str, kind_: str) -> dict:
        ref = (rels.get(name) or {}).get("data") or {}
        return (included.get((kind_, ref.get("id"))) or {}).get("attributes") or {}

    build = related("build", "builds")
    tester = related("tester", "betaTesters")
    tester_ref = ((rels.get("tester") or {}).get("data") or {})

    # Comment lives under different keys on the two resources; take the first
    # that is actually present rather than assuming either.
    comment = None
    for key in ("comment", "feedbackText", "text"):
        if attrs.get(key):
            comment = attrs[key]
            break

    name = " ".join(
        p for p in (tester.get("firstName"), tester.get("lastName")) if p
    ).strip() or None

    return {
        "submission_id": record["id"],
        "kind": kind,
        "asc_app_id": asc_app_id,
        "created_date": attrs.get("createdDate"),
        "comment": comment,
        # Apple carries no marketing version on these resources at all; the build
        # number comes off the related build. Leaving app_version null is honest -
        # do not synthesise it from the build number, they are different things.
        "app_version": None,
        "build_number": build.get("version"),
        "device_model": attrs.get("deviceModel"),
        "os_version": attrs.get("osVersion"),
        "locale": attrs.get("locale"),
        "tester_id": tester_ref.get("id"),
        "tester_name": name,
        # Often null: TestFlight public-link testers are anonymous to us.
        "tester_email": tester.get("email") or attrs.get("email"),
        # Signed URLs with an expirationDate a few weeks out. Stored so a consumer
        # can show the image, but they WILL rot - see README.
        "screenshots": attrs.get("screenshots"),
        "raw": record,
    }
