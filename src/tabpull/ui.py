import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import questionary
from rich.console import Console

console = Console()

STYLE = questionary.Style(
    [
        ('qmark', 'fg:#8ab4ff bold'),
        ('pointer', 'fg:#8ab4ff bold'),
        ('highlighted', 'fg:#8ab4ff bold'),
        ('selected', 'fg:#d1dedc'),
        ('answer', 'fg:#d1dedc bold'),
    ]
)

_sink: ContextVar[Callable[[str], None] | None] = ContextVar(
    'tabpull_sink', default=None
)
_stop: ContextVar[Callable[[], bool] | None] = ContextVar('tabpull_stop', default=None)


def emit(text: str) -> None:
    sink = _sink.get()
    if sink is None:
        print(text)
        return
    sink(text)


def stopped() -> bool:
    stop = _stop.get()
    return bool(stop and stop())


def captures_stop() -> bool:
    """True when this thread's capture() installed a stop callback."""
    return _stop.get() is not None


@contextmanager
def capture(
    sink: Callable[[str], None], stop: Callable[[], bool] | None = None
) -> Iterator[None]:
    """Send emit() lines to sink in this thread, and let stopped() see stop."""
    sink_token = _sink.set(sink)
    stop_token = _stop.set(stop)
    try:
        yield
    finally:
        _sink.reset(sink_token)
        _stop.reset(stop_token)


def rich_output() -> bool:
    return sys.stdout.isatty()


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask(question: questionary.Question) -> Any:  # noqa: ANN401 - questionary answers are untyped
    return question.unsafe_ask()


def home_path(path: Path | str) -> str:
    text = os.path.normpath(Path(path).absolute())
    home = str(Path.home())
    return '~' + text[len(home) :] if text.startswith(home + os.sep) else text
