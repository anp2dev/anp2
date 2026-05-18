"""Entry point: `python -m anp2_relay`."""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from .server import create_app
from .storage import Storage


def main() -> None:
    db_path = Path(os.environ.get("ANP2_DB", "anp2.db"))
    host = os.environ.get("ANP2_HOST", "127.0.0.1")
    port = int(os.environ.get("ANP2_PORT", "8000"))

    storage = Storage(db_path)
    app = create_app(storage)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
