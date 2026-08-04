"""Tier 2 — client logic against a scripted httpx.MockTransport backend:
login/session lifecycle, retry, dispatch, pagination, polling."""

import httpx
import pytest

from aussiebb_mcp.abb_client import (
    AussieBBClient,
    AuthenticationError,
    AussieBBError,
    RateLimitError,
    UnknownServiceError,
)
from tests.conftest import SERVICES_PAGE, read_session


class TestLoginAndSession:
    def test_lazy_login_then_persist(self, client, api):
        api.set("GET", "/services", SERVICES_PAGE)
        client.get_services()
        assert api.login_count == 1
        saved = read_session(client)
        assert saved["cookie"] == "fake-session"
        assert saved["expires_at"] > 0

    def test_api_requests_carry_session_cookie(self, client, api):
        api.set("GET", "/services", SERVICES_PAGE)
        client.get_services()
        api_cookie = api.cookie_headers[-1]
        assert "myaussie_cookie=fake-session" in api_cookie

    def test_session_reused_across_instances(self, client, api, tmp_path):
        api.set("GET", "/services", SERVICES_PAGE)
        client.get_services()
        second = AussieBBClient(
            username="user@example.com", password="hunter2",
            session_file=str(tmp_path / "session.json"),
            service_aliases={},
            transport=httpx.MockTransport(api.handler),
        )
        second.get_services()
        assert api.login_count == 1  # no second login

    def test_relogin_on_401_and_retry(self, client, api):
        api.set_sequence("GET", "/services", [
            httpx.Response(401, json={"error": "unauthenticated"}),
            httpx.Response(200, json=SERVICES_PAGE),
        ])
        services = client.get_services()
        assert len(services) == 2
        assert api.login_count == 2  # initial + re-login

    def test_login_422_raises_auth_error(self, client, api):
        api.login_response = httpx.Response(422, json={"error": "bad creds"})
        with pytest.raises(AuthenticationError, match="422"):
            client.get_services()

    def test_login_429_raises_rate_limit(self, client, api):
        api.login_response = httpx.Response(429, json={"error": "slow down"})
        with pytest.raises(RateLimitError, match="90s"):
            client.get_services()

    def test_missing_creds_clear_error(self, api, tmp_path):
        c = AussieBBClient(
            username="", password="",
            session_file=str(tmp_path / "s.json"), service_aliases={},
            transport=httpx.MockTransport(api.handler),
        )
        with pytest.raises(AuthenticationError, match="AUSSIEBB_USERNAME"):
            c.get_services()

    def test_api_429_raises_rate_limit(self, client, api):
        api.set("GET", "/services", {"error": "whoa"}, status=429)
        with pytest.raises(RateLimitError):
            client.get_services()

    def test_api_error_includes_status_and_body(self, client, api):
        api.set("GET", "/services", {"error": "boom"}, status=500)
        with pytest.raises(AussieBBError, match="500"):
            client.get_services()


class TestServices:
    def test_pagination_followed(self, client, api):
        page1 = {
            "data": [SERVICES_PAGE["data"][0]],
            "links": {"next": "https://myaussie-api.aussiebroadband.com.au/services?page=2"},
        }
        page2 = {"data": [SERVICES_PAGE["data"][1]], "links": {"next": None}}
        api.set("GET", "/services", page1)
        # second page arrives at the same path with ?page=2 — the mock routes
        # on path only, so script it as a sequence instead
        api.set_sequence("GET", "/services", [page1, page2])
        services = client.get_services()
        assert [s["service_id"] for s in services] == [1000001, 1000002]

    def test_type_filter(self, client, api):
        page = {
            "data": SERVICES_PAGE["data"] + [
                {"service_id": 999, "type": "VOIP", "plan": "Phone"},
            ],
            "links": {"next": None},
        }
        api.set("GET", "/services", page)
        assert len(client.get_services("NBN")) == 2
        assert len(client.get_services("all")) == 3


class TestServiceResolution:
    def test_alias_and_numeric_and_all(self, client):
        assert client.resolve_services("home") == [1000001]
        assert client.resolve_services("OFFICE") == [1000002]
        assert client.resolve_services("12345") == [12345]
        assert set(client.resolve_services("all")) == {1000001, 1000002}

    def test_unknown_alias_lists_known(self, client):
        with pytest.raises(UnknownServiceError, match="home"):
            client.resolve_services("bogus")

    def test_resolve_one_rejects_all(self, client):
        with pytest.raises(UnknownServiceError, match="exactly one"):
            client.resolve_one_service("all")

    def test_all_without_aliases_discovers_live(self, client, api):
        client.service_aliases = {}
        api.set("GET", "/services", SERVICES_PAGE)
        assert set(client.resolve_services("all")) == {1000001, 1000002}


class TestDispatch:
    def test_check_is_get_kick_is_post(self, client, api):
        api.set("GET", "/broadband/1000001/connection", {"connected": 1})
        api.set("POST", "/broadband/1000001/connection", {"kicked": True})
        client.check_connection(1000001)
        client.start_test(1000001, "kick_connection")
        assert ("GET", "/broadband/1000001/connection") in api.api_calls()
        assert ("POST", "/broadband/1000001/connection") in api.api_calls()

    def test_async_test_starts_with_post(self, client, api):
        api.set("POST", "/tests/1000001/ntdstatus",
                {"id": 1, "type": "NTD Status", "status": "WaitingToBeQueued"})
        record = client.start_test(1000001, "NTD Status")
        assert record["status"] == "WaitingToBeQueued"
        assert api.api_calls()[-1] == ("POST", "/tests/1000001/ntdstatus")


class TestPolling:
    def _script_run(self, api, result="Passed"):
        api.set("POST", "/tests/1000001/ntdstatus",
                {"id": 42, "type": "NTD Status",
                 "status": "WaitingToBeQueued", "result": None})
        api.set_sequence("GET", "/tests/1000001/42", [
            {"id": 42, "type": "NTD Status", "status": "WaitingToBeQueued",
             "result": None},
            {"id": 42, "type": "NTD Status", "status": "InProgress",
             "result": None},
            {"id": 42, "type": "NTD Status", "status": "Completed",
             "result": result,
             "output": {"uptime": 100, "linkState": "up",
                        "totalFlaps": 2, "lastFlapAt": None}},
        ])

    def test_wait_polls_to_completion(self, client, api):
        self._script_run(api)
        out = client.run_async_test(1000001, "ntd_status", wait=True,
                                    timeout_s=5, poll_interval=0)
        assert out["completed"] is True
        assert out["result"] == "Passed"
        assert "link up" in out["summary"]
        assert "guidance" not in out

    def test_failed_test_carries_guidance(self, client, api):
        self._script_run(api, result="Failed")
        out = client.run_async_test(1000001, "ntd_status", wait=True,
                                    timeout_s=5, poll_interval=0)
        assert out["result"] == "Failed"
        assert "flap count" in out["guidance"]

    def test_no_wait_returns_immediately(self, client, api):
        self._script_run(api)
        out = client.run_async_test(1000001, "ntd_status", wait=False,
                                    timeout_s=5)
        assert out["completed"] is False
        assert out["id"] == 42
        assert ("GET", "/tests/1000001/42") not in api.api_calls()

    def test_timeout_flags_summary(self, client, api):
        api.set("POST", "/tests/1000001/loopback",
                {"id": 7, "type": "Loopback", "status": "WaitingToBeQueued"})
        api.set("GET", "/tests/1000001/7",
                {"id": 7, "type": "Loopback", "status": "InProgress"})
        out = client.run_async_test(1000001, "loopback", wait=True,
                                    timeout_s=0, poll_interval=0)
        assert out["completed"] is False
        assert "timed out" in out["summary"]
        assert out["guidance"]
