import csv
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from functools import partial
from pathlib import Path
from typing import Protocol

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Playwright, sync_playwright
from rich import box
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.rule import Rule
from rich.table import Table

from tabpull import ui
from tabpull.embed import export_embed
from tabpull.jobs import Job, JobError
from tabpull.tableau import (
    MissingSettingsError,
    UnknownSiteError,
    browser_session,
    load_site,
)


def _first_line(message: str) -> str:
    return message.partition('\n')[0] or message


class _PlainRun:
    def __init__(self, jobs: Sequence[Job], out_dir: Path) -> None:
        print(f'Exporting {len(jobs)} job(s) to {out_dir}/')

    @staticmethod
    def live() -> AbstractContextManager[object]:
        return nullcontext()

    @staticmethod
    def sheet(job: Job, done: int, sheet: str) -> None:
        pass

    @staticmethod
    def ok(job: Job, paths: Sequence[Path]) -> None:
        print(f'  ✓ {job.name}: {", ".join(map(str, paths))}')

    @staticmethod
    def fail(job: Job, message: str) -> None:
        print(f'  ✗ {job.name}: {_first_line(message)}')

    @staticmethod
    def summary(ok: int, total: int, rerun: str | None) -> None:
        print(f'done: {ok}/{total} jobs exported')
        if rerun:
            print(f'help: fix the failed jobs, then rerun them: {rerun}')


_BAR_REASON_MAX_CHARS = 48


class _TTYRun:
    def __init__(self, jobs: Sequence[Job], out_dir: Path) -> None:
        ui.console.print(Rule('[bold]tabpull run', align='left', style='blue'))
        ui.console.print(f'Exporting {len(jobs)} job(s) to {escape(str(out_dir))}/')
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn('[bold]{task.fields[job]:<12}'),
            TextColumn('{task.description}'),
            BarColumn(bar_width=20),
            TextColumn('{task.completed}/{task.total} sheets'),
            TimeElapsedColumn(),
            console=ui.console,
        )
        self.tasks: dict[int, TaskID] = {
            id(job): self.progress.add_task(
                'queued', job=escape(job.name), total=len(job.sheets)
            )
            for job in jobs
        }
        self.rows: list[tuple[str, bool, str]] = []

    def live(self) -> AbstractContextManager[object]:
        return self.progress

    def sheet(self, job: Job, done: int, sheet: str) -> None:
        self.progress.update(
            self.tasks[id(job)],
            completed=done,
            description=f'exporting {escape(sheet)}',
        )

    def ok(self, job: Job, paths: Sequence[Path]) -> None:
        self.progress.update(
            self.tasks[id(job)], completed=len(paths), description='[green]done[/]'
        )
        self.rows.append((job.name, True, ', '.join(map(ui.home_path, paths))))

    def fail(self, job: Job, message: str) -> None:
        line = _first_line(message).strip()
        short = line and len(line) <= _BAR_REASON_MAX_CHARS
        self.progress.update(
            self.tasks[id(job)],
            description=f'[yellow]{escape(line)}[/]' if short else '[red]failed[/]',
        )
        self.rows.append((job.name, False, line))

    def summary(self, ok: int, total: int, rerun: str | None) -> None:
        table = Table(box=box.SIMPLE, show_header=False)
        for name, success, info in self.rows:
            mark = '[green]✓[/]' if success else '[red]✗[/]'
            table.add_row(mark, f'[bold]{escape(name)}[/]', escape(info))
        ui.console.print(
            Panel(
                table,
                title=f'done: {ok}/{total} jobs exported',
                title_align='left',
                border_style='green' if ok == total else 'red',
            )
        )
        if rerun:
            ui.console.print(f'  [dim]rerun failed:[/] [bold]{escape(rerun)}[/]\n')


class Report(Protocol):
    def live(self) -> AbstractContextManager[object]: ...

    def sheet(self, job: Job, done: int, sheet: str) -> None: ...

    def ok(self, job: Job, paths: Sequence[Path]) -> None: ...

    def fail(self, job: Job, message: str) -> None: ...

    def summary(self, ok: int, total: int, rerun: str | None) -> None: ...


def open_report(jobs: Sequence[Job], out_dir: Path) -> Report:
    return (_TTYRun if ui.rich_output() else _PlainRun)(jobs, out_dir)


def _fail_all(site_jobs: Sequence[Job], message: str, report: Report) -> list[str]:
    for job in site_jobs:
        report.fail(job, message)
    return [job.name for job in site_jobs]


def _export_group(
    pw: Playwright,
    site_name: str,
    site_jobs: Sequence[Job],
    out_dir: Path,
    report: Report,
) -> list[str]:
    try:
        settings = load_site(site_name)
    except (UnknownSiteError, MissingSettingsError, ValueError) as e:
        return _fail_all(site_jobs, str(e), report)
    ui.emit(
        f'  site {settings.name}: {settings.server}, site {settings.site or "(default)"}'
    )
    try:
        context = browser_session(pw, settings)
    except SystemExit as e:
        message = (
            e.code if isinstance(e.code, str) else 'could not open a browser session'
        )
        return _fail_all(site_jobs, message, report)
    failed: list[str] = []
    for job in site_jobs:
        if ui.stopped():
            failed.append(job.name)
            report.fail(job, 'Cancelled.')
            continue
        try:
            paths = export_embed(
                context,
                settings,
                job,
                out_dir,
                on_sheet=partial(report.sheet, job),
            )
        except (JobError, PlaywrightError, OSError, UnicodeError, csv.Error) as e:
            failed.append(job.name)
            report.fail(job, str(e))
        else:
            report.ok(job, paths)
    return failed


def run_jobs(jobs: Sequence[Job], out_dir: Path, report: Report) -> list[str]:
    groups: list[tuple[str, list[Job]]] = []
    for job in jobs:
        if groups and groups[-1][0] == job.site:
            groups[-1][1].append(job)
        else:
            groups.append((job.site, [job]))
    failed: list[str] = []
    if not groups:
        return failed
    # ponytail: cancel is checked between jobs, not mid-export. Close the browser to stop a stuck sheet.
    with report.live(), sync_playwright() as pw:
        for site_name, site_jobs in groups:
            if ui.stopped():
                failed += _fail_all(site_jobs, 'Cancelled.', report)
                continue
            failed += _export_group(pw, site_name, site_jobs, out_dir, report)
    return failed
