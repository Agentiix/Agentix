"""Agentix runtime server.

Runs inside the sandbox. Loads agent plugin + dataset plugin.
Orchestrates: setup → run → verify.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import Response

from agentix import __version__
from agentix.models import (
    EvalRequest,
    EvalResponse,
    ExecRequest,
    ExecResponse,
    HealthResponse,
    RunRequest,
    RunResponse,
    UploadResponse,
)
from agentix.runtime.executor import Executor

logger = logging.getLogger("agentix.runtime")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

app = FastAPI(title="agentix", version=__version__)
executor = Executor()

_agent_runner = None
_dataset_plugin = None
_agent_plugin_path: str | None = None
_dataset_plugin_path: str | None = None


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_agent_plugin(plugin_dir: str) -> None:
    global _agent_runner, _agent_plugin_path
    _agent_plugin_path = plugin_dir

    runner_path = Path(plugin_dir) / "runner.py"
    if not runner_path.exists():
        logger.warning("No runner.py in agent plugin: %s", plugin_dir)
        return

    _agent_runner = _load_module(runner_path, "agent_runner")

    bin_dir = Path(plugin_dir) / "bin"
    if bin_dir.exists():
        os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"

    logger.info("Loaded agent plugin: %s", plugin_dir)


def load_dataset_plugin(plugin_dir: str) -> None:
    global _dataset_plugin, _dataset_plugin_path
    _dataset_plugin_path = plugin_dir

    dataset_path = Path(plugin_dir) / "dataset.py"
    if not dataset_path.exists():
        logger.warning("No dataset.py in dataset plugin: %s", plugin_dir)
        return

    _dataset_plugin = _load_module(dataset_path, "dataset_plugin")
    logger.info("Loaded dataset plugin: %s", plugin_dir)


# ── Health ────────────────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(
        version=__version__,
        agent_plugin=_agent_plugin_path,
        dataset_plugin=_dataset_plugin_path,
    )


# ── Eval: setup → run → verify ───────────────────────────────────


@app.post("/eval", response_model=EvalResponse)
async def eval_agent(req: EvalRequest):
    """Full evaluation: dataset.setup → runner.run → dataset.verify."""
    if _agent_runner is None:
        raise HTTPException(status_code=503, detail="No agent plugin loaded")

    try:
        # 1. Setup
        if _dataset_plugin and hasattr(_dataset_plugin, "setup"):
            agent_input = await _dataset_plugin.setup()
            if req.agent_input:
                agent_input.update(req.agent_input)
        else:
            agent_input = req.agent_input or {}

        # 2. Run agent
        run_result = await _agent_runner.run(agent_input)

        # 3. Verify
        metrics = {}
        if _dataset_plugin and hasattr(_dataset_plugin, "verify"):
            metrics = await _dataset_plugin.verify()

    except Exception as e:
        logger.exception("Eval failed")
        raise HTTPException(status_code=500, detail=str(e))

    return EvalResponse(
        output=run_result.output,
        trajectory=run_result.trajectory.model_dump() if run_result.trajectory else None,
        metrics=metrics,
    )


# ── Run: just the agent, no dataset ──────────────────────────────


@app.post("/run", response_model=RunResponse)
async def run_agent(req: RunRequest):
    if _agent_runner is None:
        raise HTTPException(status_code=503, detail="No agent plugin loaded")

    try:
        run_result = await _agent_runner.run(req.agent_input)
    except Exception as e:
        logger.exception("Agent run failed")
        raise HTTPException(status_code=500, detail=str(e))

    return RunResponse(
        output=run_result.output,
        trajectory=run_result.trajectory.model_dump() if run_result.trajectory else None,
    )


# ── Low-level: exec, upload, download ─────────────────────────────


@app.post("/exec", response_model=ExecResponse)
async def exec_command(req: ExecRequest):
    exit_code, stdout, stderr = await executor.exec(
        command=req.command,
        timeout=req.timeout,
        cwd=req.cwd,
        extra_env=req.env,
    )
    return ExecResponse(exit_code=exit_code, stdout=stdout, stderr=stderr)


@app.post("/upload", response_model=UploadResponse)
async def upload(
    file: UploadFile = File(...),
    path: str = Form(...),
):
    data = await file.read()
    size = executor.upload(data, path)
    return UploadResponse(path=path, size=size)


@app.get("/download")
async def download(path: str):
    try:
        data = executor.download(path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Not found: {path}")
    return Response(content=data, media_type="application/octet-stream")
