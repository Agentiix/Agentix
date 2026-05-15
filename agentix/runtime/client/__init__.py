"""Orchestrator-side client for the agentix runtime.

Public surface:
  - `RuntimeClient` — connects to a running sandbox, drives unary RPC over
    HTTP and stream/bidi/log subscriptions over Socket.IO.
  - `RemoteCallError` — raised when a remote impl returns a non-ok response.

Implementation lives in `agentix.runtime.client.client`; this package's
`__init__.py` re-exports the public names so the historic import path
`from agentix.runtime.client import RuntimeClient` keeps working.
"""

from agentix.runtime.client.client import RemoteCallError, RuntimeClient

__all__ = ["RemoteCallError", "RuntimeClient"]
