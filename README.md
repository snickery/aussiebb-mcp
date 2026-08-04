# aussiebb-mcp

An [MCP](https://modelcontextprotocol.io) server for Aussie Broadband NBN
diagnostics — the same tests the
[MyAussie portal](https://my.aussiebroadband.com.au/) offers under
"All Service Tests" (Check/Kick Connection, Loopback, NTD Status/Reset),
plus test history, NBN outages, speed test results, and fault tickets.
Give your AI assistant the other half of the "is the internet down?"
question: when the LAN view says the WAN is dead, these tools say whether
it's the ISP/NBN side.

> **Unofficial API — use at your own risk.** This project is not
> affiliated with or endorsed by Aussie Broadband. It talks to the
> undocumented MyAussie JSON API (`myaussie-api.aussiebroadband.com.au`)
> — the same API the portal SPA and the Home Assistant integration
> ([pyaussiebb](https://github.com/yaleman/pyaussiebb)) use — which can
> change or break without notice. Two tools (`kick_connection`,
> `ntd_reset`) **deliberately disconnect your service**; they refuse to
> run without `confirm=true`, but understand what they do before granting
> it. Your portal credentials go into an env file — protect it
> accordingly, and use the API respectfully.

## Tools

Services are addressed by a **site alias** (optional, from
`AUSSIEBB_SERVICES`), a raw numeric service_id, or `'all'`. Read-only
tools default to `all`; test-running tools require exactly one service.

| Tool | Params | Notes |
|---|---|---|
| `list_services` | `service_type='NBN'` | ids, aliases, plan/speed/POI, WAN IPs |
| `list_diagnostics` | `service='all'` | what AussieBB offers, destructive flags |
| `check_connection` | `service='all'` | instant ISP-side "is it connected" — run this first |
| `ntd_status` | `service, wait=True, timeout_s=180` | ~2 min; link state, uptime, **flap count** (dropout evidence) |
| `loopback_test` | `service, wait=True, timeout_s=180` | ~2 min; two-way data proof to the NBN box/node |
| `test_result` | `service, test_id` | fetch one run's status/result (for `wait=False` flows) |
| `test_history` | `service='all', limit=10` | recent runs with pass/fail summaries |
| `service_outages` | `service='all'` | current + scheduled + resolved NBN/ABB outages |
| `speed_test_results` | `service='all'` | ABB-recorded speed tests |
| `list_faults` | `include_details=True` | open fault tickets (account-wide) |
| `kick_connection` | `service, confirm=False` | **DESTRUCTIVE** — drops the session (modem swaps); refuses without `confirm=true` |
| `ntd_reset` | `service, confirm=False, wait, timeout_s` | **DESTRUCTIVE** — reboots the NBN box; refuses without `confirm=true` |
| `run_diagnostic` | `test, service, wait, timeout_s, confirm` | escape hatch for any test by name; destructive names still gated |

Failed or unfinished tests carry AussieBB's own "report a fault if"
guidance in the response — the API's failure payloads are otherwise bare.

## Quick start (stdio)

Most users need exactly two env vars — services are discovered live from
the account:

```jsonc
// e.g. Claude Desktop claude_desktop_config.json / Claude Code .mcp.json
{
  "mcpServers": {
    "aussiebb": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/aussiebb-mcp", "aussiebb-mcp", "--stdio"],
      "env": {
        "AUSSIEBB_USERNAME": "you@example.com",
        "AUSSIEBB_PASSWORD": "..."
      }
    }
  }
}
```

Prefer an env file over inline credentials where your client supports it
(`uv run --env-file ...`). stdio mode has no network surface and skips
bearer auth — the client owns the process.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `AUSSIEBB_USERNAME` / `AUSSIEBB_PASSWORD` | *(empty)* | MyAussie portal login. Tools error clearly when unset. |
| `AUSSIEBB_SERVICES` | *(empty)* | Optional `alias=id` map, e.g. `home=1000001,office=1000002`. Aliases become tool vocabulary (and are baked into the server instructions); without it, services are discovered live. |
| `AUSSIEBB_SESSION_FILE` | `~/.local/state/aussiebb-mcp/session.json` | Where the ~1-year login cookie persists (0600, dir 0700). |
| `PORT` | `8329` | HTTP listen port. |
| `MCP_BEARER_TOKEN` | *(empty)* | Required in HTTP mode; server refuses to start without it. Not used in `--stdio` mode. |

## HTTP mode (container)

The bundled `Containerfile` builds a Streamable HTTP server at `/mcp`
(stateless). HTTP mode **refuses to start** without `MCP_BEARER_TOKEN`;
clients authenticate with `Authorization: Bearer <token>`.

```bash
podman build -t aussiebb-mcp .   # or: docker build -t aussiebb-mcp .
podman run -d --name aussiebb-mcp -p 8329:8329 -v abb-state:/data \
  -e AUSSIEBB_USERNAME=you@example.com -e AUSSIEBB_PASSWORD=... \
  -e AUSSIEBB_SESSION_FILE=/data/session.json \
  -e MCP_BEARER_TOKEN=some-long-random-token \
  aussiebb-mcp
```

Persist the session file across restarts (the volume above) — the login
endpoint rate-limits hard, and the cookie lives about a year.
`aussiebb_mcp.healthcheck` does a full HTTP round-trip to `/mcp` (the 401
counts as alive); wire it to your container healthcheck. Terminate TLS at
a reverse proxy — the server itself speaks plain HTTP.

## API notes (hard-won)

- **Login** (`POST myaussie-auth.../login`) returns a `myaussie_cookie`
  valid ~1 year. The endpoint is aggressively rate-limited (429) — the
  client persists the session to disk, logs in lazily, re-logs-in at most
  once per request on 401/419 (invalidating in memory only, so a failed
  re-login can't destroy a persisted cookie), and serializes login behind
  a lock. Never retry-loop a 429.
- **Check and Kick share one URL** (`/broadband/{sid}/connection`):
  GET = harmless check, POST = drops the session. Dispatch is strictly
  name → (path, method) via the `TESTS` registry; the `link` field from
  the available-tests response is never blind-POSTed.
- **Async tests** return a record immediately
  (`status: WaitingToBeQueued`) and are polled at
  `GET /tests/{sid}/{test_id}` through `InProgress` to `Completed`
  (`result: Passed|Failed`), ~115 s total in practice.

## Testing

```bash
# Tier 1+2 — pure units + mocked-transport client/server logic
# (no network, no credentials — fully fabricated fixtures)
uv run --extra test pytest tests/ --ignore=tests/test_integration.py -v

# Tier 3 — live read-only API tests (login, services, check_connection,
# history, outages; starts no test runs, changes nothing)
AUSSIEBB_TEST=1 AUSSIEBB_USERNAME=... AUSSIEBB_PASSWORD=... \
    uv run --extra test pytest tests/test_integration.py -v

# optional: one live NTD Status run (~2 min, shows up in portal history)
AUSSIEBB_TEST=1 AUSSIEBB_TEST_RUN=1 AUSSIEBB_USERNAME=... AUSSIEBB_PASSWORD=... \
    uv run --extra test pytest tests/test_integration.py -v
```

`kick_connection` and `ntd_reset` are never exercised by any automated
tier.

## License

[MIT](LICENSE)
