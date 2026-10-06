"""Windows NSSM launcher for the MobiRent farm agent."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path.cwd()
ENV_FILE = ROOT / ".env"
load_dotenv(ENV_FILE, override=False)

logger = logging.getLogger("service_launcher")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    host = os.getenv("FARM_AGENT_LISTEN_HOST", "0.0.0.0")
    port = os.getenv("FARM_AGENT_LISTEN_PORT", "8790")
    server = ROOT / "tools" / "farm_agent_status_server.py"

    logger.info("starting farm agent host=%s port=%s", host, port)

    return subprocess.call(
        [sys.executable, str(server), "--host", str(host), "--port", str(port)],
        cwd=str(ROOT),
        env=os.environ.copy(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
