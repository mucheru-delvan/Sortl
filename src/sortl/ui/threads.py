"""Run blocking work off the GTK main loop and hop back safely."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from gi.repository import GLib


def call_in_main(func: Callable[..., Any], *args: Any) -> None:
    """Schedule ``func(*args)`` on the GTK main loop (thread-safe)."""

    def trampoline() -> bool:
        func(*args)
        return GLib.SOURCE_REMOVE

    GLib.idle_add(trampoline)


def run_in_thread[T](
    work: Callable[[], T],
    on_done: Callable[[T], None],
    on_error: Callable[[Exception], None],
    *,
    daemon: bool = True,
) -> threading.Thread:
    """Run ``work`` in a thread; deliver the result on the main loop.

    Use ``daemon=False`` for work that mutates the filesystem so interpreter
    shutdown waits for it instead of killing it mid-move.
    """

    def target() -> None:
        try:
            result = work()
        except Exception as exc:  # delivered to on_error, never swallowed
            call_in_main(on_error, exc)
        else:
            call_in_main(on_done, result)

    thread = threading.Thread(target=target, name="sortl-worker", daemon=daemon)
    thread.start()
    return thread
