"""Entry point: reads AGENTIX_SOCKET from env and serves on that UDS.
No CLI args — matches the Agentix closure convention.
"""

from __future__ import annotations

import logging
import os

import uvicorn


def main() -> None:
    socket_path = os.environ["AGENTIX_SOCKET"]
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [mock-agent] %(message)s"
    )

    from mock_agent.app import app

    uvicorn.run(app, uds=socket_path, log_level="info")


if __name__ == "__main__":
    main()
