import logging

from mcp.server.fastmcp import FastMCP
from mcp_server_kit import offload_sync_tools, serve

from aussiebb_mcp.abb_client import (
    TESTS,
    AussieBBClient,
    normalize_test_name,
)
from aussiebb_mcp.config import MCP_BEARER_TOKEN, PORT


client = AussieBBClient()


def _build_instructions() -> str:
    """Server instructions, with the configured site aliases baked in so
    clients see the caller's actual vocabulary (falls back to generic
    wording when no alias map is configured)."""
    aliases = sorted(client.service_aliases)
    if aliases:
        addressing = (
            "Services are addressed by site alias: "
            + ", ".join(f"'{a}'" for a in aliases)
            + " ('all' or a numeric service_id also work; read-only tools"
            " default to all)."
        )
    else:
        addressing = (
            "Services are addressed by a numeric service_id or 'all' "
            "(read-only tools default to all; list_services shows what the "
            "account has, and AUSSIEBB_SERVICES can map friendly aliases)."
        )
    return (
        "Aussie Broadband NBN diagnostics via the MyAussie portal API — the "
        "ISP-side complement to LAN-side monitoring: when the local view "
        "says the internet is down, these tools say whether it's the "
        f"ISP/NBN side. {addressing} Start with check_connection (instant) "
        "+ service_outages; ntd_status adds dropout evidence (flap count); "
        "loopback_test proves two-way data through NBN. Slow tests take "
        "~2 minutes — wait=True polls to completion. kick_connection and "
        "ntd_reset DISCONNECT THE SERVICE — they refuse to run without "
        "confirm=true; always ask the user before setting it."
    )


mcp = FastMCP(
    "aussiebb",
    instructions=_build_instructions(),
    host="0.0.0.0",
    port=PORT,
    stateless_http=True,
    json_response=True,
)
offload_sync_tools(mcp, logger_name="aussiebb_mcp.requests")


def _service_label(sid: int) -> dict:
    return {"service_id": sid, "site": client.alias_for(sid)}


def _per_service(service: str, fn) -> list[dict]:
    """Run fn(sid) for each resolved service, labeling each result."""
    return [
        {**_service_label(sid), **fn(sid)}
        for sid in client.resolve_services(service)
    ]


_CONFIRM_REFUSAL = (
    "Refused: this disconnects the service. Call again with confirm=true "
    "after the user has explicitly approved it."
)


@mcp.tool()
def list_services(service_type: str = "NBN") -> list:
    """List services on the AussieBB account: service_id, site alias, plan,
    speed tier, address, WAN IPs. service_type: 'NBN' (default) or 'all'."""
    out = []
    for s in client.get_services(service_type):
        out.append({
            **_service_label(s["service_id"]),
            "plan": s.get("plan"),
            "description": s.get("description"),
            "product": (s.get("nbnDetails") or {}).get("product"),
            "speed": (
                f"{(s.get('nbnDetails') or {}).get('download')}/"
                f"{(s.get('nbnDetails') or {}).get('upload')} Mbps"
            ),
            "poi": (s.get("nbnDetails") or {}).get("poiName"),
            "ip_addresses": s.get("ipAddresses"),
            "type": s.get("type"),
        })
    return out


@mcp.tool()
def list_diagnostics(service: str = "all") -> list:
    """List the diagnostics AussieBB offers for a service (name, summary,
    which are destructive). service: a site alias | id | 'all'."""
    def fetch(sid: int) -> dict:
        tests = []
        for t in client.available_tests(sid):
            try:
                spec = TESTS[normalize_test_name(t["name"])]
                destructive = spec.destructive
            except Exception:
                destructive = None  # unknown new test — treat with care
            tests.append({
                "name": t["name"],
                "summary": t.get("summary"),
                "destructive": destructive,
            })
        return {"tests": tests}
    return _per_service(service, fetch)


@mcp.tool()
def check_connection(service: str = "all") -> list:
    """Instant ISP-side connectivity check: is the service connected from
    AussieBB's end? Safe, non-disruptive. THE first diagnostic to run.
    service: a site alias | service_id | 'all' (default)."""
    def fetch(sid: int) -> dict:
        connected = bool(client.check_connection(sid).get("connected"))
        return {
            "connected": connected,
            "summary": "Connected" if connected else (
                "NOT CONNECTED from AussieBB's side. "
                + TESTS["check_connection"].guidance
            ),
        }
    return _per_service(service, fetch)


@mcp.tool()
def ntd_status(service: str, wait: bool = True, timeout_s: int = 180) -> dict:
    """Run an NTD Status test (~2 min): NBN box link state, uptime, and
    dropout ('flap') count — the evidence AussieBB's fault process keys on.
    Safe, non-disruptive. service: a site alias | service_id.
    wait=False returns immediately with a test id to poll via test_result."""
    sid = client.resolve_one_service(service)
    return {**_service_label(sid),
            **client.run_async_test(sid, "ntd_status", wait, timeout_s)}


@mcp.tool()
def loopback_test(service: str, wait: bool = True, timeout_s: int = 180) -> dict:
    """Run a Loopback test (~2 min): proves data passes both ways between
    AussieBB and the NBN box/node. Safe, non-disruptive.
    service: a site alias | service_id. wait=False returns
    immediately with a test id to poll via test_result."""
    sid = client.resolve_one_service(service)
    return {**_service_label(sid),
            **client.run_async_test(sid, "loopback", wait, timeout_s)}


@mcp.tool()
def test_result(service: str, test_id: int) -> dict:
    """Fetch one test's current status/result by id (from test_history or
    a wait=False run). service: a site alias | service_id."""
    sid = client.resolve_one_service(service)
    detail = client.test_detail(sid, test_id)
    from aussiebb_mcp.abb_client import summarize_test
    return {
        **_service_label(sid),
        "summary": summarize_test(detail),
        "completed": detail.get("status") == "Completed",
        **detail,
    }


@mcp.tool()
def test_history(service: str = "all", limit: int = 10) -> list:
    """Recent diagnostic runs (type, status, Passed/Failed, timestamps).
    service: a site alias | service_id | 'all' (default)."""
    def fetch(sid: int) -> dict:
        from aussiebb_mcp.abb_client import summarize_test
        entries = client.test_history(sid)[: max(1, limit)]
        return {"tests": [
            {**e, "summary": summarize_test(e)} for e in entries
        ]}
    return _per_service(service, fetch)


@mcp.tool()
def service_outages(service: str = "all") -> list:
    """Current, scheduled, and resolved NBN/AussieBB outages for the
    service — check this before blaming local gear.
    service: a site alias | service_id | 'all' (default)."""
    def fetch(sid: int) -> dict:
        o = client.outages(sid)
        current = (
            len(o.get("networkEvents", []))
            + len(o.get("aussieOutages", []))
            + len(o.get("currentNbnOutages", []))
        )
        scheduled = len(o.get("scheduledNbnOutages", []))
        return {
            "summary": (
                f"{current} current outage(s)/event(s), {scheduled} scheduled"
                if current or scheduled else "No current or scheduled outages"
            ),
            **o,
        }
    return _per_service(service, fetch)


@mcp.tool()
def speed_test_results(service: str = "all") -> list:
    """Speed test results recorded against the service by AussieBB.
    service: a site alias | service_id | 'all' (default)."""
    return _per_service(service, lambda sid: {"speed_tests": client.speed_tests(sid)})


@mcp.tool()
def list_faults(include_details: bool = True) -> list:
    """Open fault tickets on the AussieBB account (account-wide, not
    per-service)."""
    tickets = client.tickets()
    return tickets if include_details else [
        {k: t.get(k) for k in ("id", "subject", "status")} for t in tickets
    ]


@mcp.tool()
def kick_connection(service: str, confirm: bool = False) -> dict:
    """DESTRUCTIVE: drop the service's session at AussieBB (use when
    swapping the modem/router; reconnection can take ~10 min). Refuses
    unless confirm=true — get the user's explicit approval first.
    service: a site alias | service_id (never 'all')."""
    sid = client.resolve_one_service(service)
    if not confirm:
        return {**_service_label(sid), "kicked": False,
                "refused": _CONFIRM_REFUSAL}
    result = client.start_test(sid, "kick_connection")
    return {**_service_label(sid), "kicked": True,
            "guidance": TESTS["kick_connection"].guidance, "raw": result}


@mcp.tool()
def ntd_reset(
    service: str, confirm: bool = False, wait: bool = True, timeout_s: int = 300
) -> dict:
    """DESTRUCTIVE: reboot the NBN box (minutes of outage while it
    restarts). Only useful when NTD Status errors. Refuses unless
    confirm=true — get the user's explicit approval first.
    service: a site alias | service_id (never 'all')."""
    sid = client.resolve_one_service(service)
    if not confirm:
        return {**_service_label(sid), "reset": False,
                "refused": _CONFIRM_REFUSAL}
    return {**_service_label(sid),
            **client.run_async_test(sid, "ntd_reset", wait, timeout_s)}


@mcp.tool()
def run_diagnostic(
    test: str,
    service: str,
    wait: bool = True,
    timeout_s: int = 180,
    confirm: bool = False,
) -> dict:
    """Run any diagnostic by name ('NTD Status', 'loopback', ...) —
    escape hatch equivalent to the portal's Run Tests buttons. Destructive
    tests (kick_connection, ntd_reset) still require confirm=true.
    service: a site alias | service_id."""
    sid = client.resolve_one_service(service)
    spec = TESTS[normalize_test_name(test)]
    if spec.destructive and not confirm:
        return {**_service_label(sid), "test": spec.name,
                "refused": _CONFIRM_REFUSAL}
    if not spec.is_async:
        result = (
            client.check_connection(sid) if spec.method == "GET"
            else client.start_test(sid, spec.name)
        )
        return {**_service_label(sid), "test": spec.name, "raw": result,
                "guidance": spec.guidance}
    return {**_service_label(sid),
            **client.run_async_test(sid, spec.name, wait, timeout_s)}


def main():
    logging.basicConfig(level=logging.INFO)

    serve(mcp, port=PORT, bearer_token=MCP_BEARER_TOKEN)


if __name__ == "__main__":
    main()
