"""Dispatcher registration for the files primitive.

Composes the `Files` stub class with the `FilesImpl` impl class — no
inheritance edge.
"""

from __future__ import annotations

from agentix.dispatch import Dispatcher

from . import Files
from ._impl import FilesImpl


def register() -> Dispatcher:
    return Dispatcher().bind_namespace(Files, FilesImpl())
