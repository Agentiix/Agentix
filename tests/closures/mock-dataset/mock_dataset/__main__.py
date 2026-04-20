"""Entry point: reads AGENTIX_SOCKET from env; no CLI args."""

from __future__ import annotations

import logging
import os

import uvicorn


def main() -> None:
    socket_path = os.environ["AGENTIX_SOCKET"]
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [mock-dataset] %(message)s"
    )

    from mock_dataset.app import app

    uvicorn.run(app, uds=socket_path, log_level="info")


if __name__ == "__main__":
    main()
