"""Aussie Broadband MyAussie API client for aussiebb-mcp.

Talks to the same unofficial JSON API the MyAussie portal uses
(reverse-engineered by pyaussiebb; every shape below verified live
against the real API 2026-08-02).

Design notes
------------
* **Auth**: ``POST {LOGIN_URL}`` with ``{username, password}`` sets a
  ``myaussie_cookie`` valid for ~1 year (``expiresIn`` seconds). The
  login endpoint is aggressively rate-limited (429), so the session is
  persisted to disk and reused across restarts; we log in lazily, only
  when there is no usable session, and re-login at most once per request
  on a 401/419. A lock serializes login so concurrent worker threads
  can't stampede the auth endpoint.
* **Check vs Kick share one URL** (``/broadband/{sid}/connection``):
  GET = harmless connectivity check, POST = drops the session. All test
  dispatch therefore goes through the ``TESTS`` registry keyed by test
  name, never by blind-POSTing a ``link`` from the available-tests
  response.
* **Async tests** (loopback / ntdstatus / ntdreset) return a test record
  immediately (``status: WaitingToBeQueued``); poll
  ``GET /tests/{sid}/{test_id}`` until ``Completed`` (observed
  vocabulary: WaitingToBeQueued → InProgress → Completed, ~115 s total).
  Any non-Completed status is treated as in-progress.
* Results carry AussieBB's own "report a fault if" guidance because the
  API's failure payloads are bare (a Failed NTD Reset has no detail).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass

import httpx

from aussiebb_mcp import config

API_BASE = "https://myaussie-api.aussiebroadband.com.au"
LOGIN_URL = "https://myaussie-auth.aussiebroadband.com.au/login"
COOKIE_NAME = "myaussie_cookie"

_HTTP_TIMEOUT = 30
# Re-login this far before nominal expiry; with a ~1-year cookie the
# margin is generous and still amortizes to ~zero logins.
_EXPIRY_MARGIN = 24 * 3600
_POLL_INTERVAL = 10.0

logger = logging.getLogger(__name__)


class AussieBBError(RuntimeError):
    """Base for all client errors (message is user/agent-facing)."""


class AuthenticationError(AussieBBError):
    pass


class RateLimitError(AussieBBError):
    pass


class UnknownServiceError(AussieBBError):
    pass


class UnknownTestError(AussieBBError):
    pass


@dataclass(frozen=True)
class TestSpec:
    name: str
    path: str  # format with sid=
    method: str  # "GET" or "POST"
    is_async: bool  # returns a test record to poll vs an immediate result
    destructive: bool
    guidance: str  # AussieBB's "report a fault if" advice


TESTS: dict[str, TestSpec] = {
    spec.name: spec
    for spec in (
        TestSpec(
            name="check_connection",
            path="/broadband/{sid}/connection",
            method="GET",
            is_async=False,
            destructive=False,
            guidance=(
                "Report a fault if the result shows not connected, or the "
                "Loopback Test also fails after power-cycling the NBN box."
            ),
        ),
        TestSpec(
            name="kick_connection",
            path="/broadband/{sid}/connection",
            method="POST",
            is_async=False,
            destructive=True,
            guidance=(
                "Immediately disconnects the service; reconnection can take "
                "~10 minutes. Report a fault if still not connected after a "
                "10-minute wait."
            ),
        ),
        TestSpec(
            name="loopback",
            path="/tests/{sid}/loopback",
            method="POST",
            is_async=True,
            destructive=False,
            guidance=(
                "Report a fault if the test fails after power-cycling the "
                "NBN box and the modem/router."
            ),
        ),
        TestSpec(
            name="ntd_status",
            path="/tests/{sid}/ntdstatus",
            method="POST",
            is_async=True,
            destructive=False,
            guidance=(
                "Report a fault if the test fails, or if two tests run 24h "
                "apart show the flap count increasing by 5 or more."
            ),
        ),
        TestSpec(
            name="ntd_reset",
            path="/tests/{sid}/ntdreset",
            method="POST",
            is_async=True,
            destructive=True,
            guidance=(
                "Reboots the NBN box (minutes of outage). Report a fault if "
                "NTD Status still reports a problem 10 minutes after the "
                "reset and a modem/router restart."
            ),
        ),
    )
}

# Portal display names → registry keys, so run_diagnostic("NTD Status")
# and run_diagnostic("ntdstatus") both work.
_TEST_NAME_SYNONYMS = {
    "check connection": "check_connection",
    "connection": "check_connection",
    "kick connection": "kick_connection",
    "kick": "kick_connection",
    "loopback test": "loopback",
    "loopback": "loopback",
    "ntd status": "ntd_status",
    "ntdstatus": "ntd_status",
    "ntd reset": "ntd_reset",
    "ntdreset": "ntd_reset",
}


def normalize_test_name(name: str) -> str:
    """Map any spelling of a test name to a TESTS registry key."""
    key = name.strip().lower().replace("-", " ").replace("_", " ")
    canonical = _TEST_NAME_SYNONYMS.get(key) or _TEST_NAME_SYNONYMS.get(
        key.replace(" ", "")
    )
    if canonical is None:
        known = ", ".join(sorted(TESTS))
        raise UnknownTestError(
            f"Unknown test {name!r}. Known tests: {known}. For anything "
            "else AussieBB adds later, use list_diagnostics to see what "
            "the service offers."
        )
    return canonical


def parse_service_aliases(raw: str) -> dict[str, int]:
    """Parse 'home=1000001,office=1000002' into an alias map."""
    aliases: dict[str, int] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        alias, _, sid = pair.partition("=")
        alias, sid = alias.strip().lower(), sid.strip()
        if not alias or not sid.isdigit():
            raise ValueError(
                f"Bad AUSSIEBB_SERVICES entry {pair!r} — expected alias=id"
            )
        aliases[alias] = int(sid)
    return aliases


def humanize_seconds(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def summarize_test(detail: dict) -> str:
    """One-line human summary of a test record, incl. NTD Status output."""
    kind = detail.get("type", "test")
    status = detail.get("status", "unknown")
    result = detail.get("result")
    if status != "Completed":
        return f"{kind}: {status.lower()} (not finished yet)"
    line = f"{kind}: {result}"
    output = detail.get("output") or {}
    if output.get("linkState") is not None:
        line += (
            f" — link {output['linkState']}, "
            f"uptime {humanize_seconds(output.get('uptime'))}, "
            f"{output.get('totalFlaps')} flaps"
        )
        if output.get("lastFlapAt"):
            line += f" (last {output['lastFlapAt']})"
    return line


class _Session:
    """Disk-persisted login session ({cookie, expires_at} JSON, 0600)."""

    def __init__(self, path: str):
        self.path = path
        self.cookie: str = ""
        self.expires_at: float = 0.0
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path) as f:
                data = json.load(f)
            self.cookie = data.get("cookie", "")
            self.expires_at = float(data.get("expires_at", 0))
        except (OSError, ValueError):
            pass

    def save(self, cookie: str, expires_at: float) -> None:
        self.cookie, self.expires_at = cookie, expires_at
        directory = os.path.dirname(self.path)
        if directory:
            # Owner-only: the session cookie inside is a ~1-year credential.
            os.makedirs(directory, mode=0o700, exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            json.dump({"cookie": cookie, "expires_at": expires_at}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def usable(self) -> bool:
        return bool(self.cookie) and time.time() < self.expires_at - _EXPIRY_MARGIN


class AussieBBClient:
    """Thread-safe MyAussie API client (login serialized by a lock)."""

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        session_file: str | None = None,
        service_aliases: dict[str, int] | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self.username = username if username is not None else config.AUSSIEBB_USERNAME
        self.password = password if password is not None else config.AUSSIEBB_PASSWORD
        self.session = _Session(session_file or config.SESSION_FILE)
        self.service_aliases = (
            service_aliases
            if service_aliases is not None
            else parse_service_aliases(config.AUSSIEBB_SERVICES)
        )
        self._http = httpx.Client(timeout=_HTTP_TIMEOUT, transport=transport)
        self._login_lock = threading.Lock()
        self._apply_cookie()

    def _apply_cookie(self) -> None:
        """Rebuild the HTTP cookie jar from the persisted session (the one
        source of truth) — httpx deprecated per-request cookies."""
        self._http.cookies.clear()
        if self.session.cookie:
            self._http.cookies.set(
                COOKIE_NAME, self.session.cookie,
                domain=".aussiebroadband.com.au",
            )

    # -- auth --------------------------------------------------------------

    def _require_creds(self) -> None:
        if not self.username or not self.password:
            raise AuthenticationError(
                "AUSSIEBB_USERNAME / AUSSIEBB_PASSWORD are not set. Add them "
                "to the env file (see README § Setup) and restart."
            )

    def login(self) -> None:
        """Authenticate and persist the session. Serialized by a lock; a
        thread that waited on the lock reuses the fresh session instead of
        logging in again."""
        self._require_creds()
        with self._login_lock:
            if self.session.usable():
                return
            resp = self._http.post(
                LOGIN_URL,
                json={"username": self.username, "password": self.password},
            )
            if resp.status_code == 422:
                raise AuthenticationError(
                    "AussieBB rejected the credentials (HTTP 422). Check "
                    "AUSSIEBB_USERNAME / AUSSIEBB_PASSWORD."
                )
            if resp.status_code == 429:
                raise RateLimitError(
                    "AussieBB is rate-limiting logins (HTTP 429). Wait ~90s "
                    "and retry; do not retry-loop."
                )
            resp.raise_for_status()
            cookie = resp.cookies.get(COOKIE_NAME, "")
            expires_in = resp.json().get("expiresIn")
            if not cookie or not expires_in:
                raise AuthenticationError(
                    "Login response was missing the session cookie or expiry "
                    "— the auth API may have changed."
                )
            self.session.save(cookie, time.time() + float(expires_in))
            self._apply_cookie()
            logger.info(
                "Logged in to MyAussie; session valid ~%s",
                humanize_seconds(int(expires_in)),
            )

    def _request(self, method: str, path: str, *, _retry: bool = True) -> httpx.Response:
        if not self.session.usable():
            self.login()
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        resp = self._http.request(method, url)
        if resp.status_code in (401, 419) and _retry:
            logger.info("Session rejected (HTTP %d) — re-logging in", resp.status_code)
            # Invalidate in memory only: a successful re-login overwrites the
            # persisted file, but if the login itself fails (e.g. 429) the
            # on-disk cookie survives for the next process to try.
            self.session.cookie, self.session.expires_at = "", 0.0
            self._apply_cookie()
            return self._request(method, path, _retry=False)
        if resp.status_code == 429:
            raise RateLimitError(
                "AussieBB is rate-limiting API requests (HTTP 429). Back off "
                "before retrying."
            )
        if resp.is_error:
            raise AussieBBError(
                f"MyAussie API error {resp.status_code} for {method} {path}: "
                f"{resp.text[:300]}"
            )
        return resp

    def _get(self, path: str) -> dict | list:
        return self._request("GET", path).json()

    def _post(self, path: str) -> dict | list:
        return self._request("POST", path).json()

    # -- services ----------------------------------------------------------

    def get_services(self, service_type: str = "NBN") -> list[dict]:
        """All services on the account (paginated), optionally filtered by
        type ('NBN', 'Opticomm', ...; '' or 'all' = every service)."""
        services: list[dict] = []
        url: str | None = "/services"
        while url:
            page = self._get(url)
            services.extend(page.get("data", []))
            url = (page.get("links") or {}).get("next")
        if service_type and service_type.lower() != "all":
            services = [
                s for s in services
                if s.get("type", "").lower() == service_type.lower()
            ]
        return services

    def alias_for(self, service_id: int) -> str | None:
        for alias, sid in self.service_aliases.items():
            if sid == service_id:
                return alias
        return None

    def resolve_services(self, service: str) -> list[int]:
        """Resolve a service selector to service_ids.

        Accepts a configured alias (e.g. 'home'), a numeric service_id, or
        'all' (every NBN service — from the alias map when configured,
        else discovered live)."""
        sel = str(service).strip().lower()
        if not sel:
            raise UnknownServiceError(
                "service is required — pass an alias, a service_id, or 'all'."
            )
        if sel == "all":
            if self.service_aliases:
                return list(self.service_aliases.values())
            return [s["service_id"] for s in self.get_services("NBN")]
        if sel.isdigit():
            return [int(sel)]
        if sel in self.service_aliases:
            return [self.service_aliases[sel]]
        known = ", ".join(sorted(self.service_aliases)) or "(no aliases configured)"
        raise UnknownServiceError(
            f"Unknown service {service!r}. Known aliases: {known}; a numeric "
            "service_id or 'all' also works (see list_services)."
        )

    def resolve_one_service(self, service: str) -> int:
        """Like resolve_services but for tools that act on exactly one."""
        sids = self.resolve_services(service)
        if len(sids) != 1:
            raise UnknownServiceError(
                "This tool needs exactly one service — pass a single alias "
                "or service_id, not 'all'."
            )
        return sids[0]

    # -- diagnostics -------------------------------------------------------

    def available_tests(self, sid: int) -> list[dict]:
        return self._get(f"/tests/{sid}/available")

    def test_history(self, sid: int) -> list[dict]:
        return self._get(f"/tests/{sid}")

    def test_detail(self, sid: int, test_id: int) -> dict:
        return self._get(f"/tests/{sid}/{test_id}")

    def check_connection(self, sid: int) -> dict:
        return self._get(f"/broadband/{sid}/connection")

    def start_test(self, sid: int, test: str) -> dict:
        """Start a test by registry name and return the initial record.
        Dispatches strictly on the TESTS registry (name → path + method) —
        never on the 'link' field — because check and kick share a URL."""
        spec = TESTS[normalize_test_name(test)]
        resp = self._request(spec.method, spec.path.format(sid=sid))
        return resp.json()

    def wait_for_test(
        self,
        sid: int,
        test_id: int,
        timeout_s: float = 180,
        poll_interval: float = _POLL_INTERVAL,
    ) -> tuple[dict, bool]:
        """Poll a test until Completed or timeout → (detail, timed_out).
        Runs in a worker thread under the server, so sleeping is fine."""
        deadline = time.monotonic() + timeout_s
        while True:
            detail = self.test_detail(sid, test_id)
            if detail.get("status") == "Completed":
                return detail, False
            if time.monotonic() >= deadline:
                return detail, True
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))

    def run_async_test(
        self, sid: int, test: str, wait: bool, timeout_s: float,
        poll_interval: float = _POLL_INTERVAL,
    ) -> dict:
        """Start an async test; optionally wait for completion. Returns a
        normalized record with summary + guidance."""
        spec = TESTS[normalize_test_name(test)]
        record = self.start_test(sid, test)
        timed_out = False
        if wait and record.get("id"):
            record, timed_out = self.wait_for_test(
                sid, record["id"], timeout_s, poll_interval
            )
        return self.normalize_test_record(record, sid, spec, timed_out)

    @staticmethod
    def normalize_test_record(
        record: dict, sid: int, spec: TestSpec, timed_out: bool = False
    ) -> dict:
        out = {
            "service_id": sid,
            "test": spec.name,
            "summary": summarize_test(record),
            "completed": record.get("status") == "Completed",
            **{
                k: record.get(k)
                for k in ("id", "type", "status", "result", "created_at",
                          "completed_at", "output")
                if k in record
            },
        }
        if timed_out:
            out["summary"] += (
                " — polling timed out; fetch again with test_result "
                f"(test_id={record.get('id')})"
            )
        if record.get("result") == "Failed" or not out["completed"]:
            out["guidance"] = spec.guidance
        return out

    # -- bonus endpoints ---------------------------------------------------

    def outages(self, sid: int) -> dict:
        return self._get(f"/nbn/{sid}/outages")

    def speed_tests(self, sid: int) -> list[dict]:
        return self._get(f"/speedtests/{sid}")

    def tickets(self) -> list[dict]:
        return self._get("/tickets")
