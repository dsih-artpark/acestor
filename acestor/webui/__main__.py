"""Entrypoint — ``python -m acestor.webui``.

Precedence for host/port/log-level:  CLI flag  >  env var  >  default.
"""

from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m acestor.webui")
    p.add_argument(
        "--host",
        default=os.environ.get("WEB_UI_HOST", "127.0.0.1"),
        help="Bind host (default: 127.0.0.1). Set to 0.0.0.0 to expose beyond "
        "localhost — only do this behind a reverse proxy or on a trusted network.",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("WEB_UI_PORT", "8000")),
        help="Bind port (default: 8000). Env: WEB_UI_PORT.",
    )
    p.add_argument(
        "--log-level",
        default=os.environ.get("WEB_UI_LOG_LEVEL", "info"),
        choices=["critical", "error", "warning", "info", "debug", "trace"],
    )
    args = p.parse_args()

    # Import late so --help doesn't require FastAPI to be installed.
    from acestor.webui.app import app

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
