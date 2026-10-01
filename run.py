"""Start E.V.:  python run.py

Settings come from .env (see .env.example):
  EV_HOST=127.0.0.1   laptop only (default)   |  EV_HOST=0.0.0.0  also reachable from your phone on the same Wi-Fi
  EV_PORT=8000
  EV_SSL_CERTFILE / EV_SSL_KEYFILE  optional HTTPS (needed for the phone microphone; see README)
"""
from __future__ import annotations

import logging
import sys

if sys.version_info < (3, 10):
    sys.exit("E.V. needs Python 3.10 or newer. Download it from https://www.python.org/downloads/")

import uvicorn  # noqa: E402

from backend import config  # noqa: E402
from backend.integrations.system_monitor import lan_addresses  # noqa: E402


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    scheme = "https" if config.SSL_CERTFILE and config.SSL_KEYFILE else "http"
    print("\n  E.V. is starting…")
    print(f"  On this laptop:  {scheme}://localhost:{config.PORT}")
    if config.HOST in ("0.0.0.0", "::"):
        for ip in lan_addresses():
            print(f"  On your phone:   {scheme}://{ip}:{config.PORT}   (same Wi-Fi)")
    else:
        print("  Phone access is off. Set EV_HOST=0.0.0.0 in .env to enable it.")
    print("  Press Ctrl+C to stop.\n")
    uvicorn.run("backend.main:app", host=config.HOST, port=config.PORT, log_level="info",
                ssl_certfile=config.SSL_CERTFILE, ssl_keyfile=config.SSL_KEYFILE, proxy_headers=False)


if __name__ == "__main__":
    main()
