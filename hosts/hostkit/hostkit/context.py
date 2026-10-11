"""The task a thread is working on, propagated to every external and model call."""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator

TASK_HEADER = "x-kintsugi-task"
_task: contextvars.ContextVar[str] = contextvars.ContextVar("kintsugi_task", default="")


def current_task() -> str:
    return _task.get()


@contextlib.contextmanager
def task_context(task_id: str) -> Iterator[str]:
    token = _task.set(task_id)
    try:
        yield task_id
    finally:
        _task.reset(token)


def task_headers() -> dict[str, str]:
    task = current_task()
    return {TASK_HEADER: task} if task else {}
