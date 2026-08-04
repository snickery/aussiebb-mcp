"""Tier 2 — server-side confirm gating: destructive tools must refuse
without confirm=True and must not touch the API when refusing. The
threadpool decorator returns the original sync fn, so tools are directly
callable here."""

from unittest.mock import MagicMock

import pytest

import aussiebb_mcp.server as server


@pytest.fixture
def mock_client(monkeypatch):
    c = MagicMock(name="client")
    c.resolve_one_service.return_value = 1000001
    c.alias_for.return_value = "home"
    monkeypatch.setattr(server, "client", c)
    return c


class TestConfirmGating:
    def test_kick_refused_without_confirm(self, mock_client):
        out = server.kick_connection("home")
        assert out["kicked"] is False and "confirm=true" in out["refused"]
        mock_client.start_test.assert_not_called()

    def test_kick_runs_with_confirm(self, mock_client):
        mock_client.start_test.return_value = {"ok": True}
        out = server.kick_connection("home", confirm=True)
        assert out["kicked"] is True
        mock_client.start_test.assert_called_once_with(1000001, "kick_connection")

    def test_ntd_reset_refused_without_confirm(self, mock_client):
        out = server.ntd_reset("home")
        assert out["reset"] is False and "confirm=true" in out["refused"]
        mock_client.run_async_test.assert_not_called()

    def test_ntd_reset_runs_with_confirm(self, mock_client):
        mock_client.run_async_test.return_value = {"completed": True}
        out = server.ntd_reset("home", confirm=True)
        mock_client.run_async_test.assert_called_once()

    def test_run_diagnostic_gates_destructive(self, mock_client):
        for test in ("kick connection", "NTD Reset"):
            out = server.run_diagnostic(test, "home")
            assert "refused" in out
        mock_client.start_test.assert_not_called()
        mock_client.run_async_test.assert_not_called()

    def test_run_diagnostic_safe_needs_no_confirm(self, mock_client):
        mock_client.check_connection.return_value = {"connected": 1}
        out = server.run_diagnostic("check connection", "home")
        assert out["raw"] == {"connected": 1}
        mock_client.check_connection.assert_called_once_with(1000001)


class TestReadOnlyTools:
    def test_check_connection_summarizes_disconnect(self, mock_client):
        mock_client.resolve_services.return_value = [1000001]
        mock_client.check_connection.return_value = {"connected": 0}
        out = server.check_connection("home")
        assert out[0]["connected"] is False
        assert "NOT CONNECTED" in out[0]["summary"]

    def test_service_outages_summary_counts(self, mock_client):
        mock_client.resolve_services.return_value = [1000001]
        mock_client.outages.return_value = {
            "networkEvents": [], "aussieOutages": [{"id": 1}],
            "currentNbnOutages": [], "resolvedNbnOutages": [],
            "scheduledNbnOutages": [{"id": 2}],
            "resolvedScheduledNbnOutages": [],
        }
        out = server.service_outages("home")
        assert "1 current" in out[0]["summary"]
        assert "1 scheduled" in out[0]["summary"]
