"""Tier 1 — pure unit tests: name/alias parsing, summaries, no HTTP."""

import pytest

from aussiebb_mcp.abb_client import (
    TESTS,
    UnknownTestError,
    humanize_seconds,
    normalize_test_name,
    parse_service_aliases,
    summarize_test,
)


class TestNormalizeTestName:
    @pytest.mark.parametrize("spelling,expected", [
        ("Check Connection", "check_connection"),
        ("check_connection", "check_connection"),
        ("connection", "check_connection"),
        ("Kick Connection", "kick_connection"),
        ("Loopback Test", "loopback"),
        ("loopback", "loopback"),
        ("NTD Status", "ntd_status"),
        ("ntdstatus", "ntd_status"),
        ("NTD-Reset", "ntd_reset"),
        ("ntdreset", "ntd_reset"),
    ])
    def test_spellings(self, spelling, expected):
        assert normalize_test_name(spelling) == expected

    def test_unknown_raises_with_known_list(self):
        with pytest.raises(UnknownTestError, match="ntd_status"):
            normalize_test_name("frobnicate")

    def test_all_registry_names_round_trip(self):
        for name in TESTS:
            assert normalize_test_name(name) == name


class TestDestructiveFlags:
    def test_exactly_kick_and_reset_are_destructive(self):
        destructive = {n for n, s in TESTS.items() if s.destructive}
        assert destructive == {"kick_connection", "ntd_reset"}

    def test_check_and_kick_share_url_but_not_method(self):
        check, kick = TESTS["check_connection"], TESTS["kick_connection"]
        assert check.path == kick.path
        assert (check.method, kick.method) == ("GET", "POST")


class TestParseServiceAliases:
    def test_parses_map(self):
        assert parse_service_aliases("home=1000001, office=1000002") == {
            "home": 1000001, "office": 1000002,
        }

    def test_empty_ok(self):
        assert parse_service_aliases("") == {}

    def test_bad_entry_raises(self):
        with pytest.raises(ValueError, match="alias=id"):
            parse_service_aliases("home=notanumber")


class TestHumanizeSeconds:
    @pytest.mark.parametrize("seconds,expected", [
        (None, "unknown"),
        (59, "0m"),
        (3660, "1h 1m"),
        (871718, "10d 2h"),
    ])
    def test_cases(self, seconds, expected):
        assert humanize_seconds(seconds) == expected


class TestSummarizeTest:
    def test_ntd_status_with_output(self):
        detail = {
            "type": "NTD Status", "status": "Completed", "result": "Passed",
            "output": {"uptime": 871718, "linkState": "up",
                       "totalFlaps": 10, "lastFlapAt": "2026-07-23 12:46:01"},
        }
        s = summarize_test(detail)
        assert "NTD Status: Passed" in s
        assert "link up" in s and "10 flaps" in s and "10d 2h" in s

    def test_bare_pass_fail(self):
        assert summarize_test(
            {"type": "Loopback", "status": "Completed", "result": "Passed"}
        ) == "Loopback: Passed"

    def test_in_progress(self):
        s = summarize_test({"type": "Loopback", "status": "InProgress"})
        assert "not finished" in s
