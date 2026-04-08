"""ATIF: Agent Trajectory Interchange Format (v1.4)

Standardized format for logging agent interaction histories.
Compatible with Harbor's ATIF spec. Useful for RL training,
evaluation analysis, and agent debugging.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AgentInfo(BaseModel):
    name: str
    version: str
    model_name: str
    extra: dict | None = None


class ToolCall(BaseModel):
    tool_call_id: str
    function_name: str
    arguments: dict


class ObservationResult(BaseModel):
    source_call_id: str
    content: str


class Observation(BaseModel):
    results: list[ObservationResult]


class Metrics(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int | None = None
    cost_usd: float = 0.0
    logprobs: list[float] | None = None
    completion_token_ids: list[int] | None = None
    prompt_token_ids: list[int] | None = None


class Step(BaseModel):
    step_id: int
    timestamp: str
    source: str = Field(description="user | agent | system")
    message: str
    reasoning_content: str | None = None
    model_name: str | None = None
    tool_calls: list[ToolCall] | None = None
    observation: Observation | None = None
    metrics: Metrics | None = None
    extra: dict | None = None


class FinalMetrics(BaseModel):
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cached_tokens: int = 0
    total_cost_usd: float = 0.0
    total_steps: int = 0


class Trajectory(BaseModel):
    schema_version: str = "ATIF-v1.4"
    session_id: str
    agent: AgentInfo
    steps: list[Step] = Field(default_factory=list)
    final_metrics: FinalMetrics = Field(default_factory=FinalMetrics)
    extra: dict | None = None

    def add_step(self, step: Step) -> None:
        self.steps.append(step)
        if step.metrics:
            self.final_metrics.total_prompt_tokens += step.metrics.prompt_tokens
            self.final_metrics.total_completion_tokens += step.metrics.completion_tokens
            self.final_metrics.total_cached_tokens += step.metrics.cached_tokens or 0
            self.final_metrics.total_cost_usd += step.metrics.cost_usd
        self.final_metrics.total_steps = len(self.steps)
