import os
import sys
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
