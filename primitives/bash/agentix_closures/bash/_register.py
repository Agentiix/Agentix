"""Dispatcher registration for the bash primitive.

Composes the `Bash` stub class with the `BashImpl` impl class. No
inheritance edge between them — `bind_namespace` looks up each public
method on the impl instance by name (see `Dispatcher.bind_namespace`).
"""

from __future__ import annotations

from agentix.dispatch import Dispatcher

from . import Bash
from ._impl import BashImpl


def register() -> Dispatcher:
    return Dispatcher().bind_namespace(Bash, BashImpl())
