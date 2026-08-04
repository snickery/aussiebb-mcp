"""Shared fixtures for aussiebb-mcp tests.

Tier 2 tests use httpx.MockTransport: the `api` fixture is a scriptable
fake MyAussie backend with a call log, so tests assert on exactly which
requests went out (method + path) and inject canned responses — including
login, 401-retry, and polling sequences.
"""

import json
import os

import httpx
import pytest

LOGIN_HOST = "myaussie-auth.aussiebroadband.com.au"
API_HOST = "myaussie-api.aussiebroadband.com.au"

# Fully fabricated services: fake ids, example addresses, TEST-NET-3 IPs.
SERVICES_PAGE = {
    "data": [
        {"service_id": 1000001, "type": "NBN", "plan": "HFC Hyperfast",
         "description": "NBN: UN 1, 10 EXAMPLE ST",
         "nbnDetails": {"product": "HFC", "download": 2000, "upload": 100,
                        "poiName": "Exchange West"},
         "ipAddresses": ["203.0.113.10"]},
        {"service_id": 1000002, "type": "NBN", "plan": "Ultrafast",
         "description": "NBN: UN 2, 20 SAMPLE AVE",
         "nbnDetails": {"product": "HFC", "download": 1000, "upload": 100,
                        "poiName": "Exchange East"},
         "ipAddresses": ["203.0.113.20"]},
    ],
    "links": {"next": None},
}


class FakeAPI:
    """Routes (METHOD, path) → a canned response or a list popped in order."""

    def __init__(self):
        self.routes = {}
        self.calls = []  # (method, host, path)
        self.cookie_headers = []  # cookie header per call, same order
        self.login_count = 0
        self.login_response = httpx.Response(
            200,
            json={"expiresIn": 31535999},
            headers={"set-cookie": "myaussie_cookie=fake-session; Path=/"},
        )

    def set(self, method, path, response, *, status=200):
        if not isinstance(response, httpx.Response):
            response = httpx.Response(status, json=response)
        self.routes[(method, path)] = response

    def set_sequence(self, method, path, responses):
        self.routes[(method, path)] = [
            r if isinstance(r, httpx.Response) else httpx.Response(200, json=r)
            for r in responses
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        self.calls.append((request.method, host, path))
        self.cookie_headers.append(request.headers.get("cookie", ""))
        if host == LOGIN_HOST:
            self.login_count += 1
            return self.login_response
        entry = self.routes.get((request.method, path))
        if entry is None:
            return httpx.Response(404, json={"error": f"no route {path}"})
        if isinstance(entry, list):
            return entry.pop(0) if len(entry) > 1 else entry[0]
        return entry

    def api_calls(self):
        return [(m, p) for (m, h, p) in self.calls if h == API_HOST]


@pytest.fixture
def api():
    return FakeAPI()


@pytest.fixture
def client(api, tmp_path):
    from aussiebb_mcp.abb_client import AussieBBClient

    return AussieBBClient(
        username="user@example.com",
        password="hunter2",
        session_file=str(tmp_path / "session.json"),
        service_aliases={"home": 1000001, "office": 1000002},
        transport=httpx.MockTransport(api.handler),
    )


def read_session(client):
    with open(client.session.path) as f:
        return json.load(f)


requires_live = pytest.mark.skipif(
    not (
        os.environ.get("AUSSIEBB_TEST")
        and os.environ.get("AUSSIEBB_USERNAME")
        and os.environ.get("AUSSIEBB_PASSWORD")
    ),
    reason="Set AUSSIEBB_TEST=1 + AUSSIEBB_USERNAME/PASSWORD for live tests",
)
