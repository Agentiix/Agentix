from __future__ import annotations

import pickle
from typing import Any

from agentix.runtime.shared.callables import RemoteCallable
from agentix.runtime.shared.models import RemoteRequest


def request_for(
    fn: Any,
    *,
    args: list[Any] | None = None,
    kwargs: dict[str, Any] | None = None,
    call_id: str | None = None,
) -> RemoteRequest:
    return RemoteRequest(
        callable=RemoteCallable._resolve(fn),
        arguments=pickle.dumps((tuple(args or ()), dict(kwargs or {}))),
        call_id=call_id,
    )
