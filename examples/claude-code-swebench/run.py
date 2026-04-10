"""End-to-end: Claude Code on SWE-bench via closure protocol.

Demonstrates: load closures → setup → run → verify → unload.

Usage:
    # Start runtime server
    python -m agentix.runtime --port 8000 &

    # Run on a single instance
    python examples/claude-code-swebench/run.py \
        --server http://localhost:8000 \
        --agent ../Agentix-Agents-Hub/claude-code \
        --dataset ../Agentix-Datasets/swebench \
        --instance-file instance.json

    # instance.json (from preprocess.py):
    # {"instance_id": "django__django-16139", "problem_statement": "...", "repo": "django/django"}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

from agentix.runtime.client import RuntimeClient

logger = logging.getLogger("example")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
)


async def run_pipeline(
    server_url: str,
    agent_path: str,
    dataset_path: str,
    instance: dict,
    eval_script: str | None,
    output: str,
):
    t0 = time.monotonic()

    async with RuntimeClient(server_url) as client:
        await client.wait_until_alive(timeout=30)
        logger.info("Connected to runtime server")

        # 1. Load closures
        logger.info("Loading closures...")
        await client.load(agent_path, namespace="claude")
        await client.load(dataset_path, namespace="swebench")
        logger.info("  closures loaded")

        try:
            # 2. Setup — dataset returns instruction
            t = time.monotonic()
            agent_input = await client.call("swebench", "setup", {"instance": instance})
            logger.info("  setup (%.1fs): %s...",
                        time.monotonic() - t,
                        agent_input.get("instruction", "")[:80])

            # 3. Run — agent executes
            t = time.monotonic()
            agent_output = await client.call("claude", "run", agent_input)
            logger.info("  agent (%.1fs): exit_code=%s, patch=%d chars",
                        time.monotonic() - t,
                        agent_output.get("exit_code"),
                        len(agent_output.get("patch", "")))

            # 4. Verify — dataset evaluates
            t = time.monotonic()
            verify_data = {
                "instance": instance,
                "agent_output": agent_output,
            }
            if eval_script:
                verify_data["eval_script"] = eval_script
            verify_result = await client.call("swebench", "verify", verify_data)
            logger.info("  verify (%.1fs): pass=%s, reason=%s",
                        time.monotonic() - t,
                        verify_result.get("pass"),
                        verify_result.get("reason", ""))

        finally:
            await client.unload("claude")
            await client.unload("swebench")

    # Write result
    result = {
        "instance_id": instance.get("instance_id"),
        "agent_input": agent_input,
        "agent_output": agent_output,
        "verify": verify_result,
        "elapsed": round(time.monotonic() - t0, 1),
    }

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, default=str))
    logger.info("Result → %s (%.1fs total)", output, time.monotonic() - t0)


def main():
    parser = argparse.ArgumentParser(description="Run Claude Code on SWE-bench")
    parser.add_argument("--server", default="http://localhost:8000")
    parser.add_argument("--agent", required=True, help="Path to agent closure (e.g. ../Agentix-Agents-Hub/claude-code)")
    parser.add_argument("--dataset", required=True, help="Path to dataset closure (e.g. ../Agentix-Datasets/swebench)")
    parser.add_argument("--instance-file", required=True, help="JSON file with SWE-bench instance")
    parser.add_argument("--eval-script", default=None, help="Path to eval.sh for verification")
    parser.add_argument("--output", default="result.json")
    args = parser.parse_args()

    instance = json.loads(Path(args.instance_file).read_text())
    eval_script = Path(args.eval_script).read_text() if args.eval_script else None

    asyncio.run(run_pipeline(args.server, args.agent, args.dataset, instance, eval_script, args.output))


if __name__ == "__main__":
    main()
