"""Eval CLI: runs inside the sandbox.

Usage:
    python -m agentix.eval --agent /opt/agent [--dataset /opt/dataset] [--output /output/result.json]

Orchestrates: dataset.setup(ctx) → runner.run(ctx) → dataset.verify(ctx)
All functions receive and return plain dicts. No agentix import required in plugins.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("agentix.eval")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def run_eval(agent_dir: str, dataset_dir: str | None, output_path: str) -> dict:
    # Load agent plugin
    runner_path = Path(agent_dir) / "runner.py"
    if not runner_path.exists():
        raise FileNotFoundError(f"No runner.py in {agent_dir}")
    runner = _load_module(runner_path, "agent_runner")

    # Add agent bin/ to PATH
    bin_dir = Path(agent_dir) / "bin"
    if bin_dir.exists():
        os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"

    # Load dataset plugin (optional)
    dataset = None
    if dataset_dir:
        dataset_path = Path(dataset_dir) / "dataset.py"
        if dataset_path.exists():
            dataset = _load_module(dataset_path, "dataset_plugin")

    # Build initial context
    ctx = {
        "agent_dir": agent_dir,
        "dataset_dir": dataset_dir,
        "workdir": os.getcwd(),
    }

    # 1. Setup — dataset prepares environment, returns agent input
    if dataset and hasattr(dataset, "setup"):
        logger.info("dataset.setup()")
        setup_result = await dataset.setup(ctx)
        ctx.update(setup_result)

    # 2. Run — agent executes
    logger.info("runner.run()")
    run_result = await runner.run(ctx)
    ctx["run_result"] = run_result

    # 3. Verify — dataset collects metrics
    metrics = {}
    if dataset and hasattr(dataset, "verify"):
        logger.info("dataset.verify()")
        metrics = await dataset.verify(ctx)

    # Build output
    result = {
        "output": run_result,
        "metrics": metrics,
    }

    # Write
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str))
    logger.info("Result → %s", output_path)

    return result


def main():
    parser = argparse.ArgumentParser(description="agentix eval")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--output", default="/output/result.json")
    args = parser.parse_args()

    asyncio.run(run_eval(args.agent, args.dataset, args.output))


if __name__ == "__main__":
    main()
