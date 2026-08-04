"""Tier 3 — live MyAussie API tests. Read-only + the instant Check
Connection GET; nothing here starts a test run or changes state, so there
is no cleanup. kick_connection and ntd_reset are NEVER exercised by any
automated tier — they get one supervised manual run at rollout.

Run with:
    AUSSIEBB_TEST=1 uv run --env-file ~/.config/mcp/aussiebb.env \
        --extra test pytest tests/test_integration.py -v

Optionally set AUSSIEBB_TEST_RUN=1 to also run one live NTD Status test
(safe, but takes ~2 minutes and appears in the portal's history).
"""

import os

import pytest

from tests.conftest import requires_live


@pytest.fixture(scope="module")
def live_client(tmp_path_factory):
    from aussiebb_mcp.abb_client import AussieBBClient

    return AussieBBClient(
        session_file=str(tmp_path_factory.mktemp("session") / "session.json"),
        service_aliases={},
    )


@requires_live
class TestLiveReadOnly:
    def test_services_lists_nbn(self, live_client):
        services = live_client.get_services("NBN")
        assert services, "expected at least one NBN service"
        assert all("service_id" in s for s in services)

    def test_available_tests_include_the_big_five(self, live_client):
        sid = live_client.get_services("NBN")[0]["service_id"]
        names = {t["name"] for t in live_client.available_tests(sid)}
        assert {"Check Connection", "Kick Connection", "Loopback Test",
                "NTD Status", "NTD Reset"} <= names

    def test_check_connection(self, live_client):
        sid = live_client.get_services("NBN")[0]["service_id"]
        result = live_client.check_connection(sid)
        assert "connected" in result

    def test_history_and_detail(self, live_client):
        sid = live_client.get_services("NBN")[0]["service_id"]
        history = live_client.test_history(sid)
        if not history:
            pytest.skip("no test history on this service")
        detail = live_client.test_detail(sid, history[0]["id"])
        assert detail["id"] == history[0]["id"]
        assert "status" in detail

    def test_outages_shape(self, live_client):
        sid = live_client.get_services("NBN")[0]["service_id"]
        outages = live_client.outages(sid)
        assert "currentNbnOutages" in outages

    def test_speed_tests_and_tickets(self, live_client):
        sid = live_client.get_services("NBN")[0]["service_id"]
        assert isinstance(live_client.speed_tests(sid), list)
        assert isinstance(live_client.tickets(), list)


@requires_live
@pytest.mark.skipif(
    not os.environ.get("AUSSIEBB_TEST_RUN"),
    reason="Set AUSSIEBB_TEST_RUN=1 to run a live NTD Status test (~2 min)",
)
class TestLiveTestRun:
    def test_ntd_status_run(self, live_client):
        sid = live_client.get_services("NBN")[0]["service_id"]
        out = live_client.run_async_test(sid, "ntd_status", wait=True,
                                         timeout_s=240)
        assert out["completed"], f"test did not finish: {out['summary']}"
        assert out["result"] in ("Passed", "Failed")
