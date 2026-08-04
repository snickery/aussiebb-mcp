import os

# MyAussie portal credentials. The server starts fine without them — every
# tool call then returns a clear setup error — so deployment can precede
# the secrets landing.
AUSSIEBB_USERNAME = os.environ.get("AUSSIEBB_USERNAME", "")
AUSSIEBB_PASSWORD = os.environ.get("AUSSIEBB_PASSWORD", "")

# Optional site-alias → service_id map, e.g. "home=1000001,office=1000002".
# Aliases are how humans (and agents) name services; raw numeric
# service_ids are always accepted, and with no map configured services
# are discovered live from the account.
AUSSIEBB_SERVICES = os.environ.get("AUSSIEBB_SERVICES", "")

# Where the login session (1-year myaussie_cookie) is persisted across
# restarts. AussieBB rate-limits the login endpoint hard, so reusing the
# session matters. In the container this points at the mounted data dir.
SESSION_FILE = os.environ.get(
    "AUSSIEBB_SESSION_FILE",
    os.path.expanduser("~/.local/state/aussiebb-mcp/session.json"),
)

PORT = int(os.environ.get("PORT", "8329"))
MCP_BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")
