import tomllib
from collections.abc import Sequence
from pathlib import Path

import questionary
from rich.markup import escape
from rich.panel import Panel

from tabpull import ui
from tabpull.jobs import Job, JobError, delete_jobs, saved_jobs_or_exit


def _saved(jobs: Sequence[Job]) -> str:
    return ', '.join(job.name for job in jobs)


def _pick(jobs: Sequence[Job]) -> list[str]:
    picked = ui.ask(
        questionary.checkbox(
            'Jobs to remove',
            choices=[
                questionary.Choice(
                    f'{job.name}  {job.site}  {job.view}', value=job.name
                )
                for job in jobs
            ],
            style=ui.STYLE,
        )
    )
    if not picked:
        return []
    shown = ', '.join(picked)
    if not ui.ask(
        questionary.confirm(f'Remove {shown}?', default=False, style=ui.STYLE)
    ):
        return []
    return list(picked)


def _names_to_remove(jobs: Sequence[Job], names: Sequence[str]) -> list[str]:
    if names:
        return list(names)
    if not ui.interactive():
        msg = f'Pass job names to remove. Saved jobs: {_saved(jobs)}'
        raise SystemExit(msg)
    return _pick(jobs)


def _report(path: Path, removed: Sequence[Job]) -> None:
    names = ', '.join(job.name for job in removed)
    if not ui.rich_output():
        print(f'Removed {names} from {path}')
        return
    lines = [
        f'[bold]{escape(job.name)}[/]  {escape(job.site)}  {escape(job.view)}'
        for job in removed
    ]
    lines.append(escape(str(path)))
    ui.console.print(
        Panel(
            '\n'.join(lines),
            title='[green]✓ Removed[/]',
            title_align='left',
            border_style='green',
        )
    )


def _rewrite(path: Path, jobs: Sequence[Job], names: Sequence[str]) -> None:
    drop = set(names)
    removed = [job for job in jobs if job.name in drop]
    try:
        delete_jobs(path, names)
    except JobError as e:
        raise SystemExit(str(e)) from e
    except (tomllib.TOMLDecodeError, OSError) as e:
        msg = f'{path}: {e}'
        raise SystemExit(msg) from e
    _report(path, removed)


def remove_jobs(path: Path, names: Sequence[str]) -> None:
    jobs = saved_jobs_or_exit(path)
    chosen = _names_to_remove(jobs, names)
    if not chosen:
        print('Nothing removed.')
        return
    _rewrite(path, jobs, chosen)
