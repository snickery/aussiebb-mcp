"""Container liveness probe (wire it to your container healthcheck).

A full HTTP round-trip to /mcp within a short timeout proves the event
loop is alive; the bearer middleware's 401 to this unauthenticated
loopback probe is a completed response and counts as healthy. Transport
failure or timeout exits non-zero, so a restart-on-unhealthy policy
self-heals a wedged loop.
"""

import sys
import urllib.error
import urllib.request

from aussiebb_mcp.config import PORT


def main() -> None:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/mcp", timeout=5)
    except urllib.error.HTTPError:
        return
    except Exception as exc:  # noqa: BLE001 - any transport failure = unhealthy
        print(f"health: unreachable on :{PORT}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
