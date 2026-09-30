import csv
import multiprocessing
import os
import signal
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import AbstractContextManager, contextmanager, nullcontext
from functools import partial
from multiprocessing.process import BaseProcess
from multiprocessing.queues import Queue as ProcessQueue
from pathlib import Path
from queue import Empty
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
from tabpull.jobs import Job, JobError, slug
from tabpull.tableau import (
    MissingSettingsError,
    UnknownSiteError,
    browser_session,
    close_on_stop,
    load_site,
)


def _first_line(message: str) -> str:
    return message.partition('\n')[0] or message


@contextmanager
def _hold_sigint() -> Iterator[None]:
    # KeyboardInterrupt inside a blocked sync Playwright call sticks driver
    # shutdown, so Ctrl-C never returns to the shell. Hold the signal here.
    # The driver is in this process group and still closes the browser; the
    # interrupt is delivered once that shutdown has finished.
    block = getattr(signal, 'pthread_sigmask', None)
    if block is None or threading.current_thread() is not threading.main_thread():
        yield
        return
    block(signal.SIG_BLOCK, {signal.SIGINT})
    try:
        yield
    finally:
        block(signal.SIG_UNBLOCK, {signal.SIGINT})


def _interrupted() -> bool:
    if ui.stopped():
        return True
    if threading.current_thread() is not threading.main_thread():
        return False
    pending = getattr(signal, 'sigpending', None)
    return bool(pending and signal.SIGINT in pending())


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
    except (SystemExit, PlaywrightError) as e:
        if _interrupted():
            return _fail_all(site_jobs, 'Cancelled.', report)
        if isinstance(e, PlaywrightError):
            raise
        message = (
            e.code if isinstance(e.code, str) else 'could not open a browser session'
        )
        return _fail_all(site_jobs, message, report)
    failed: list[str] = []
    for job in site_jobs:
        if _interrupted():
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
            report.fail(job, 'Cancelled.' if _interrupted() else str(e))
        else:
            report.ok(job, paths)
    return failed


_POLL_S = 0.1
_CANCEL_JOIN_S = 5.0


class _Stop(Protocol):
    def set(self) -> None: ...

    def is_set(self) -> bool: ...


class _Running:
    def __init__(self) -> None:
        self.cancel = threading.Event()


def _existing_dir(path: Path) -> Path:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            return current
        current = parent
    return current if current.is_dir() else current.parent


def _probe_same(directory: Path, name: str, other: str) -> bool | None:
    if name == other:
        return False
    probe = directory / name
    try:
        probe.write_bytes(b'')
    except OSError:
        return None
    try:
        return (directory / other).exists()
    finally:
        probe.unlink(missing_ok=True)


def _probe_directory(path: Path) -> Path:
    anchor = _existing_dir(path)
    temp = Path(tempfile.gettempdir())
    try:
        if (
            temp.is_dir()
            and anchor.exists()
            and temp.stat().st_dev == anchor.stat().st_dev
        ):
            return temp
    except OSError:
        return anchor
    return anchor


def _volume_folds(path: Path) -> tuple[bool, bool]:
    """Case and Unicode-normalization folding for the volume that holds path."""
    directory = _probe_directory(path)
    token = f'.tabpull-{os.getpid()}-{time.monotonic_ns()}'
    case = _probe_same(directory, f'{token}-A', f'{token}-a')
    composed = 'é'
    decomposed = unicodedata.normalize('NFD', composed)
    norm = _probe_same(directory, f'{token}-{composed}', f'{token}-{decomposed}')
    ignores_case = os.name == 'nt' if case is None else case
    return ignores_case, bool(norm)


def _output_key(path: Path, *, ignores_case: bool, ignores_norm: bool) -> str:
    text = os.path.normpath(os.fspath(path))
    if ignores_norm:
        text = unicodedata.normalize('NFC', text)
    if ignores_case:
        text = text.casefold()
    return text


def shared_outputs(jobs: Sequence[Job], out_dir: Path) -> list[str]:
    """CSV paths that two jobs in this run would write at the same time."""
    owners: dict[str, int] = {}
    clashes: list[str] = []
    ignores_case, ignores_norm = _volume_folds(out_dir) if jobs else (False, False)
    for index, job in enumerate(jobs):
        for sheet in job.sheets:
            path = out_dir / slug(job.name) / f'{slug(sheet)}.csv'
            key = _output_key(
                path, ignores_case=ignores_case, ignores_norm=ignores_norm
            )
            previous = owners.get(key)
            if previous is None:
                owners[key] = index
                continue
            if previous == index:
                continue
            other = jobs[previous].name
            clashes.append(f'{path} ({other}, {job.name})')
    return clashes


def _open_sessions(jobs: Sequence[Job]) -> dict[str, str]:
    """Open each site's saved session once, before jobs start their own browsers."""
    errors: dict[str, str] = {}
    names = list(dict.fromkeys(job.site for job in jobs))
    if not names:
        return errors
    with sync_playwright() as pw, close_on_stop():
        for site_name in names:
            if _interrupted():
                for name in names:
                    errors.setdefault(name, 'Cancelled.')
                return errors
            error = _open_site(pw, site_name)
            if error is not None:
                errors[site_name] = error
    return errors


def _open_site(pw: Playwright, site_name: str) -> str | None:
    try:
        settings = load_site(site_name)
    except (UnknownSiteError, MissingSettingsError, ValueError) as e:
        return str(e)
    ui.emit(
        f'  site {settings.name}: {settings.server}, site {settings.site or "(default)"}'
    )
    try:
        context = browser_session(pw, settings)
    except (SystemExit, PlaywrightError) as e:
        if _interrupted():
            return 'Cancelled.'
        if isinstance(e, PlaywrightError):
            raise
        if isinstance(e.code, str):
            return e.code
        return 'could not open a browser session'
    else:
        context.close()
        return None


def _isolated_export(
    job: Job,
    out_dir: str,
    events: ProcessQueue[tuple[object, ...]],
    stop: _Stop,
) -> None:
    # Runs in its own process: its own sync driver, browser, and context.
    # The parent's stop event is polled from a thread so a blocked download
    # still dies with the browser instead of running on after Ctrl-C.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    stopped = {'on': False}

    def watch() -> None:
        while not stopped['on']:
            if stop.is_set():
                stopped['on'] = True
                return
            time.sleep(_POLL_S)

    watcher = threading.Thread(target=watch, name='tabpull-job-cancel', daemon=True)
    watcher.start()
    try:
        with ui.capture(lambda text: events.put(('note', text)), lambda: stopped['on']):
            paths = _export_own_browser(job, Path(out_dir), events)
    except SystemExit as e:
        message = (
            e.code if isinstance(e.code, str) else 'could not open a browser session'
        )
        events.put(('err', message))
    except (
        JobError,
        PlaywrightError,
        OSError,
        UnicodeError,
        csv.Error,
        UnknownSiteError,
        MissingSettingsError,
        ValueError,
    ) as e:
        events.put(('err', 'Cancelled.' if stopped['on'] else str(e)))
    else:
        events.put(('ok', [str(path) for path in paths]))
    finally:
        stopped['on'] = True


def _export_own_browser(
    job: Job, out_dir: Path, events: ProcessQueue[tuple[object, ...]]
) -> list[Path]:
    settings = load_site(job.site)

    def on_sheet(done: int, sheet: str) -> None:
        events.put(('sheet', done, sheet))

    with sync_playwright() as pw, close_on_stop():
        context = browser_session(pw, settings, sign_in=False)
        return export_embed(context, settings, job, out_dir, on_sheet=on_sheet)


def _export_job_isolated(
    job: Job,
    out_dir: Path,
    on_sheet: Callable[[int, str], None],
    running: _Running,
) -> list[Path]:
    if running.cancel.is_set():
        msg = 'Cancelled.'
        raise JobError(msg)
    ctx = multiprocessing.get_context('spawn')
    events = ctx.Queue()
    stop = ctx.Event()
    proc = ctx.Process(
        target=_isolated_export,
        args=(job, str(out_dir), events, stop),
        name=f'tabpull-{slug(job.name)}',
    )
    proc.start()
    try:
        return _collect_export(proc, events, stop, on_sheet, running)
    finally:
        stop.set()
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=_CANCEL_JOIN_S)
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=1)
        events.cancel_join_thread()
        events.close()


class _ExportResult:
    def __init__(self) -> None:
        self.paths: list[str] | None = None
        self.error: str | None = None

    @property
    def done(self) -> bool:
        return self.paths is not None or self.error is not None


def _note_cancel(
    running: _Running, stop: _Stop, deadline: float | None
) -> float | None:
    if not running.cancel.is_set():
        return deadline
    stop.set()
    if deadline is None:
        return time.monotonic() + _CANCEL_JOIN_S
    if time.monotonic() > deadline:
        msg = 'Cancelled.'
        raise JobError(msg)
    return deadline


def _poll_event(events: ProcessQueue[tuple[object, ...]]) -> tuple[object, ...] | None:
    try:
        return events.get(timeout=_POLL_S)
    except Empty:
        return None


def _record_event(
    item: tuple[object, ...],
    on_sheet: Callable[[int, str], None],
    result: _ExportResult,
) -> None:
    kind = item[0]
    if kind == 'sheet':
        _record_sheet(item, on_sheet)
        return
    if kind == 'note' and len(item) > 1:
        ui.emit(str(item[1]))
        return
    if kind == 'ok' and len(item) > 1 and isinstance(item[1], list):
        result.paths = [str(path) for path in item[1]]
        return
    if kind == 'err' and len(item) > 1:
        result.error = str(item[1])


def _record_sheet(
    item: tuple[object, ...], on_sheet: Callable[[int, str], None]
) -> None:
    try:
        _kind, done, sheet = item
    except ValueError:
        return
    if isinstance(done, int) and isinstance(sheet, str):
        on_sheet(done, sheet)


def _collect_export(
    proc: BaseProcess,
    events: ProcessQueue[tuple[object, ...]],
    stop: _Stop,
    on_sheet: Callable[[int, str], None],
    running: _Running,
) -> list[Path]:
    result = _ExportResult()
    deadline: float | None = None
    while not result.done:
        deadline = _note_cancel(running, stop, deadline)
        alive = proc.is_alive()
        item = _poll_event(events)
        if item is None:
            if not alive:
                break
            continue
        _record_event(item, on_sheet, result)
    proc.join(timeout=_CANCEL_JOIN_S)
    return _paths_or_error(proc, result, running)


def _paths_or_error(
    proc: BaseProcess, result: _ExportResult, running: _Running
) -> list[Path]:
    if result.error is not None:
        raise JobError(result.error)
    if result.paths is None:
        msg = (
            'Cancelled.'
            if running.cancel.is_set()
            else f'export stopped ({proc.exitcode})'
        )
        raise JobError(msg)
    return [Path(path) for path in result.paths]


def _run_parallel(
    jobs: Sequence[Job],
    out_dir: Path,
    report: Report,
    parallel: int,
) -> list[str]:
    with _hold_sigint(), report.live():
        errors = _open_sessions(jobs)
        failed_ids: set[int] = set()
        ready: list[Job] = []
        for job in jobs:
            error = errors.get(job.site)
            if error is None:
                ready.append(job)
                continue
            report.fail(job, error)
            failed_ids.add(id(job))
        if ready and not _interrupted():
            failed_ids.update(_export_ready(ready, out_dir, report, parallel))
        elif ready:
            failed_ids.update(set(_fail_ids(ready, 'Cancelled.', report)))
    return [job.name for job in jobs if id(job) in failed_ids]


def _fail_ids(jobs: Sequence[Job], message: str, report: Report) -> list[int]:
    _fail_all(jobs, message, report)
    return [id(job) for job in jobs]


def _export_ready(
    jobs: Sequence[Job],
    out_dir: Path,
    report: Report,
    parallel: int,
) -> set[int]:
    running = _Running()
    report_lock = threading.Lock()
    failed: set[int] = set()

    def note(text: str) -> None:
        with report_lock:
            print(text)

    def work(job: Job) -> None:
        def on_sheet(done: int, sheet: str) -> None:
            with report_lock:
                report.sheet(job, done, sheet)

        try:
            with ui.capture(note):
                paths = _export_job_isolated(job, out_dir, on_sheet, running)
        except (JobError, PlaywrightError, OSError, UnicodeError, csv.Error) as e:
            message = 'Cancelled.' if running.cancel.is_set() else str(e)
            with report_lock:
                report.fail(job, message)
                failed.add(id(job))
        else:
            with report_lock:
                report.ok(job, paths)

    futures: dict[Future[None], Job] = {}
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {pool.submit(work, job): job for job in jobs}
        pending = set(futures)
        while pending:
            if _interrupted():
                running.cancel.set()
                break
            _done, pending = wait(pending, timeout=_POLL_S, return_when=FIRST_COMPLETED)
        if running.cancel.is_set():
            _cancel_pending(futures, pending, report, report_lock, failed)
        else:
            for future in futures:
                future.result()
    return failed


def _cancel_pending(
    futures: dict[Future[None], Job],
    pending: set[Future[None]],
    report: Report,
    report_lock: threading.Lock,
    failed: set[int],
) -> None:
    for future in pending:
        job = futures[future]
        if not future.cancel():
            continue
        with report_lock:
            report.fail(job, 'Cancelled.')
        failed.add(id(job))
    for future in futures:
        if not future.cancelled():
            future.result()


def run_jobs(
    jobs: Sequence[Job],
    out_dir: Path,
    report: Report,
    *,
    parallel: int = 1,
) -> list[str]:
    if parallel < 1:
        msg = 'parallel must be at least 1'
        raise JobError(msg)
    if parallel == 1:
        return _run_sequential(jobs, out_dir, report)
    clashes = shared_outputs(jobs, out_dir)
    if clashes:
        msg = 'these jobs would write the same file at the same time:\n' + '\n'.join(
            clashes
        )
        raise JobError(msg)
    return _run_parallel(jobs, out_dir, report, parallel)


def _run_sequential(jobs: Sequence[Job], out_dir: Path, report: Report) -> list[str]:
    groups: list[tuple[str, list[Job]]] = []
    for job in jobs:
        if groups and groups[-1][0] == job.site:
            groups[-1][1].append(job)
        else:
            groups.append((job.site, [job]))
    failed: list[str] = []
    if not groups:
        return failed
    with _hold_sigint(), report.live(), sync_playwright() as pw, close_on_stop():
        for site_name, site_jobs in groups:
            if _interrupted():
                failed += _fail_all(site_jobs, 'Cancelled.', report)
                continue
            failed += _export_group(pw, site_name, site_jobs, out_dir, report)
    return failed
