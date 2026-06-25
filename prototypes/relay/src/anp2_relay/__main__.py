"""Entry point: `python -m anp2_relay`."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import uvicorn

from .server import create_app
from .storage import Storage

logger = logging.getLogger("anp2_relay")


def main() -> None:
    db_path = Path(os.environ.get("ANP2_DB", "anp2.db"))
    host = os.environ.get("ANP2_HOST", "127.0.0.1")
    port = int(os.environ.get("ANP2_PORT", "8000"))

    if host != "127.0.0.1":
        logging.basicConfig(level=logging.INFO)
        logger.warning(
            "ANP2_HOST=%s — relay is binding to a non-loopback address. "
            "In Phase 0/1 the relay has NO HTTP auth; rely on a reverse proxy "
            "(Caddy basic-auth) or a firewall to gate this port.",
            host,
        )

    storage = Storage(db_path)
    app = create_app(storage)
    # access_log=False: Caddy fronts the relay and is the (redacted) access-log
    # source of truth. uvicorn's access log records the full path+query, which
    # would write `?ctoken=<token>` into journald; disable it so the concierge
    # token is never logged at this tier. App-level logging is unaffected.
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
