"""Runtime mode context for chat vs environment requests."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class EnvironmentContext:
    root_dir: str
    archive_id: str
    group_id: str
    user_id: str
    project_key: str
    project_name: str = ""


_runtime_mode_var: ContextVar[str] = ContextVar("runtime_mode", default="chat")
_environment_var: ContextVar[EnvironmentContext | None] = ContextVar(
    "environment_context",
    default=None,
)


def current_runtime_mode() -> str:
    return _runtime_mode_var.get("chat") or "chat"


def current_environment() -> EnvironmentContext | None:
    return _environment_var.get(None)


def is_environment_mode() -> bool:
    return current_runtime_mode() == "environment" and current_environment() is not None


@contextmanager
def runtime_context(
    mode: str,
    environment: EnvironmentContext | None = None,
) -> Iterator[None]:
    mode_token = _runtime_mode_var.set(mode or "chat")
    env_token = _environment_var.set(environment)
    try:
        yield
    finally:
        _environment_var.reset(env_token)
        _runtime_mode_var.reset(mode_token)
