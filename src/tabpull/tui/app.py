import shlex
import subprocess  # noqa: S404
import sys
import tomllib
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Label, Static

from tabpull import ui
from tabpull.add import view_from_flag
from tabpull.filters import format_filter
from tabpull.home import site_rows, sso_badge
from tabpull.jobs import (
    Job,
    JobError,
    RangeFilter,
    ValuesFilter,
    delete_jobs,
    load_jobs,
    update_job,
)
from tabpull.tableau import list_sites
from tabpull.tui.forms import (
    FORM_CSS,
    ConfirmScreen,
    FilterForm,
    ParamForm,
    SheetsForm,
    TextForm,
)

CSS = (
    FORM_CSS
    + """
.section { text-style: bold; color: $accent; margin-top: 1; padding-left: 1; }
.info { padding-left: 1; }
#status { dock: top; height: 1; padding-left: 1; background: $panel; }
DataTable { height: auto; max-height: 50%; }
#jobs, #filters, #params { max-height: 1fr; }
"""
)
_MARK = '●'


def _load_error(path: Path) -> tuple[list[Job], str | None]:
    try:
        return load_jobs(path), None
    except (JobError, tomllib.TOMLDecodeError, OSError) as e:
        return [], f'{ui.home_path(path)} is unreadable: {e}'


def _row(table: DataTable[Any]) -> int | None:
    return table.cursor_row if table.row_count else None


def _refill(
    table: DataTable[Any],
    rows: list[tuple[object, ...]],
    keys: list[str] | None = None,
) -> None:
    cursor = table.cursor_row
    table.clear()
    for i, row in enumerate(rows):
        table.add_row(*row, key=keys[i] if keys else None)
    if rows:
        table.move_cursor(row=min(cursor, len(rows) - 1))


def _kind_value(item: ValuesFilter | RangeFilter) -> tuple[str, str]:
    if isinstance(item, RangeFilter):
        return 'range', f'{item.min or "(open)"} .. {item.max or "(open)"}'
    return 'values', ' | '.join(item.values)


def _save_error(e: Exception) -> JobError:
    return e if isinstance(e, JobError) else JobError(str(e))


class HomeScreen(Screen[None]):
    app: 'TabpullApp'

    BINDINGS = [
        Binding('enter', 'open', 'open', priority=True),
        Binding('space', 'mark', 'mark'),
        Binding('r', 'run', 'run'),
        Binding('a', 'add', 'add'),
        Binding('d', 'remove', 'remove'),
        Binding('s', 'setup', 'setup'),
        Binding('l', 'login', 'login'),
        Binding('g', 'reload', 'reload'),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.jobs: list[Job] = []
        self.marked: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Label('Sites', classes='section')
        yield DataTable(id='sites', cursor_type='none')
        yield Label('Jobs', classes='section')
        yield Static('', id='jobs-error', classes='info')
        yield DataTable(id='jobs', cursor_type='row', zebra_stripes=True)
        yield Static('', id='status')
        yield Footer()

    def on_mount(self) -> None:
        sites = self.query_one('#sites', DataTable)
        sites.add_columns('site', 'server', 'tableau site', 'sso')
        sites.can_focus = False
        self.query_one('#jobs', DataTable).add_columns(
            ' ', 'job', 'site', 'view', 'sheets', 'filters', 'params'
        )
        self.reload()
        self.query_one('#jobs', DataTable).focus()

    def on_screen_resume(self) -> None:
        self.reload()

    def reload(self) -> None:
        sites = self.query_one('#sites', DataTable)
        sites.clear()
        rows = site_rows()
        for name, server, site in rows:
            sites.add_row(name, server, site, Text.from_markup(sso_badge(name, server)))
        if not rows:
            sites.add_row('none yet', 'press s to set up a site', '', '')
        self.jobs, error = _load_error(self.app.jobs_file)
        names = {job.name for job in self.jobs}
        self.marked &= names
        error_line = self.query_one('#jobs-error', Static)
        error_line.update(escape(error or ''))
        error_line.display = bool(error)
        _refill(
            self.query_one('#jobs', DataTable),
            [
                (
                    _MARK if job.name in self.marked else '',
                    job.name,
                    job.site,
                    job.view,
                    ', '.join(job.sheets),
                    str(len(job.filters)),
                    str(len(job.params)),
                )
                for job in self.jobs
            ],
            keys=[job.name for job in self.jobs],
        )
        self.query_one('#status', Static).update(
            f'[bold]tabpull[/]   jobs {escape(ui.home_path(self.app.jobs_file))}   '
            f'exports {escape(ui.home_path(self.app.out_dir))}/<job>/<sheet>.csv'
            + (f'   {escape(self.app.last_run)}' if self.app.last_run else '')
        )

    def _current(self) -> Job | None:
        table = self.query_one('#jobs', DataTable)
        if not self.jobs or table.cursor_row >= len(self.jobs):
            return None
        return self.jobs[table.cursor_row]

    def _selected(self) -> list[str]:
        if self.marked:
            return [job.name for job in self.jobs if job.name in self.marked]
        current = self._current()
        return [current.name] if current else []

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == 'jobs':
            self.action_open()

    def action_open(self) -> None:
        if job := self._current():
            self.app.push_screen(JobScreen(job))

    def action_mark(self) -> None:
        if job := self._current():
            self.marked ^= {job.name}
            table = self.query_one('#jobs', DataTable)
            table.update_cell(
                job.name,
                table.ordered_columns[0].key,
                _MARK if job.name in self.marked else '',
            )
            table.move_cursor(row=table.cursor_row + 1)

    def action_run(self) -> None:
        if names := self._selected():
            self.app.run_jobs(names)
            self.reload()
        else:
            self.notify('No jobs to run. Press a to add one.', severity='warning')

    def action_remove(self) -> None:
        names = self._selected()
        if not names:
            return

        def confirmed(yes: bool | None) -> None:
            if not yes:
                return
            try:
                delete_jobs(self.app.jobs_file, names)
            except (JobError, tomllib.TOMLDecodeError, OSError) as e:
                self.notify(str(e), severity='error')
            else:
                self.marked -= set(names)
                self.notify(f'Removed {", ".join(names)}')
            self.reload()

        self.app.push_screen(
            ConfirmScreen(f'Remove {", ".join(names)} from the jobs file?'), confirmed
        )

    def action_add(self) -> None:
        self.app.shell('add')
        self.reload()

    def action_setup(self) -> None:
        self.app.shell('setup')
        self.reload()

    def action_login(self) -> None:
        self.app.shell('login')
        self.reload()

    def action_reload(self) -> None:
        self.reload()


class JobScreen(Screen[None]):
    app: 'TabpullApp'

    BINDINGS = [
        Binding('escape', 'back', 'back'),
        Binding('enter,e', 'edit', 'edit', priority=True),
        Binding('n', 'new', 'new'),
        Binding('d', 'delete', 'delete'),
        Binding('s', 'sheets', 'sheets'),
        Binding('N', 'rename', 'rename'),
        Binding('v', 'view', 'view'),
        Binding('S', 'site', 'site'),
        Binding('r', 'run', 'run'),
    ]

    def __init__(self, job: Job) -> None:
        super().__init__()
        self.job = job

    def compose(self) -> ComposeResult:
        yield Static('', id='summary', classes='info')
        yield Label('Filters  (n new, e/enter edit, d delete)', classes='section')
        yield DataTable(id='filters', cursor_type='row', zebra_stripes=True)
        yield Label('Parameters  (n new, e/enter edit, d delete)', classes='section')
        yield DataTable(id='params', cursor_type='row', zebra_stripes=True)
        yield Static('', id='status')
        yield Footer()

    def on_mount(self) -> None:
        self.query_one('#filters', DataTable).add_columns(
            'field', 'sheet', 'kind', 'value'
        )
        self.query_one('#params', DataTable).add_columns('parameter', 'value')
        self.render_job()
        self.query_one('#filters', DataTable).focus()

    def render_job(self) -> None:
        job = self.job
        sheets = '\n'.join(f'  {escape(sheet)}' for sheet in job.sheets)
        self.query_one('#summary', Static).update(
            f'[bold]{escape(job.name)}[/]  (N rename)\n'
            f'site    {escape(job.site)}  (S to change)\n'
            f'view    {escape(job.view)}  (v to change)\n'
            f'sheets  (s to edit)\n{sheets}'
        )
        _refill(
            self.query_one('#filters', DataTable),
            [
                (item.field, item.sheet or f'({job.sheets[0]})', *_kind_value(item))
                for item in job.filters
            ],
        )
        _refill(self.query_one('#params', DataTable), list(job.params.items()))
        self.query_one('#status', Static).update(
            f'[bold]tabpull[/] > {escape(self.job.name)}   '
            f'changes save to {escape(ui.home_path(self.app.jobs_file))} right away'
        )

    def commit(self, job: Job) -> None:
        try:
            update_job(self.app.jobs_file, self.job.name, job)
        except (JobError, tomllib.TOMLDecodeError, OSError) as e:
            raise _save_error(e) from e
        self.job = job
        self.render_job()
        self.notify(f'Saved {job.name}')

    def _focused_table(self) -> DataTable[Any] | None:
        focused = self.focused
        return focused if isinstance(focused, DataTable) else None

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        self.action_edit()

    def _filter_form(self, index: int | None) -> None:
        item = None if index is None else self.job.filters[index]

        def save(new: ValuesFilter | RangeFilter) -> None:
            filters = list(self.job.filters)
            if index is None:
                filters.append(new)
            else:
                filters[index] = new
            self.commit(replace(self.job, filters=filters))

        title = 'New filter' if item is None else f'Edit filter {item.field}'
        self.app.push_screen(FilterForm(title, item, self.job.sheets[0], save))

    def _param_form(self, index: int | None) -> None:
        names = list(self.job.params)
        old = None if index is None else names[index]

        def save(pair: tuple[str, str]) -> None:
            name, value = pair
            if name != old and name in self.job.params:
                msg = f'parameter {name!r} is already set on this job'
                raise JobError(msg)
            items = [
                (name, value) if k == old else (k, v)
                for k, v in self.job.params.items()
            ]
            if old is None:
                items.append((name, value))
            self.commit(replace(self.job, params=dict(items)))

        value = '' if old is None else self.job.params[old]
        title = 'New parameter' if old is None else f'Edit parameter {old}'
        self.app.push_screen(ParamForm(title, old or '', value, save))

    def action_edit(self) -> None:
        table = self._focused_table()
        if table is None or (row := _row(table)) is None:
            return
        if table.id == 'filters':
            self._filter_form(row)
        else:
            self._param_form(row)

    def action_new(self) -> None:
        table = self._focused_table()
        if table is not None and table.id == 'params':
            self._param_form(None)
        else:
            self._filter_form(None)

    def action_delete(self) -> None:
        table = self._focused_table()
        if table is None or (row := _row(table)) is None:
            return
        if table.id == 'filters':
            label = format_filter(self.job.filters[row])
            new = replace(
                self.job,
                filters=[f for i, f in enumerate(self.job.filters) if i != row],
            )
        else:
            key = list(self.job.params)[row]
            label = f'parameter {key}'
            new = replace(
                self.job, params={k: v for k, v in self.job.params.items() if k != key}
            )

        def confirmed(yes: bool | None) -> None:
            if not yes:
                return
            try:
                self.commit(new)
            except JobError as e:
                self.notify(str(e), severity='error')

        self.app.push_screen(ConfirmScreen(f'Delete {label}?'), confirmed)

    def action_sheets(self) -> None:
        self.app.push_screen(
            SheetsForm(
                f'Sheets for {self.job.name}',
                list(self.job.sheets),
                lambda sheets: self.commit(replace(self.job, sheets=sheets)),
            )
        )

    def action_rename(self) -> None:
        self.app.push_screen(
            TextForm(
                'Rename job',
                'Job name (exports go to <out>/<name>/)',
                self.job.name,
                lambda name: self.commit(replace(self.job, name=name)),
            )
        )

    def action_view(self) -> None:
        self.app.push_screen(
            TextForm(
                'Change view',
                'Workbook/View, or a view URL (sheets and filters stay as they are)',
                self.job.view,
                lambda text: self.commit(replace(self.job, view=view_from_flag(text))),
            )
        )

    def action_site(self) -> None:
        def save(name: str) -> None:
            if name not in (sites := list_sites()):
                msg = f'No site named {name!r}. Configured sites: {", ".join(sites) or "(none)"}'
                raise JobError(msg)
            self.commit(replace(self.job, site=name))

        self.app.push_screen(
            TextForm(
                'Change site', 'Local site name from tabpull setup', self.job.site, save
            )
        )

    def action_run(self) -> None:
        self.app.run_jobs([self.job.name])

    def action_back(self) -> None:
        self.app.pop_screen()


class TabpullApp(App[None]):
    TITLE = 'tabpull'
    CSS = CSS
    BINDINGS = [
        Binding('q', 'quit', 'quit'),
        Binding('question_mark', 'help', 'help'),
    ]

    def __init__(self, jobs_file: Path, out_dir: Path) -> None:
        super().__init__()
        self.jobs_file = jobs_file
        self.out_dir = out_dir
        self.last_run = ''

    def on_mount(self) -> None:
        self.push_screen(HomeScreen())

    def action_help(self) -> None:
        if self.screen.query('HelpPanel'):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()

    def shell(self, *argv: str) -> int:
        """Leave the full screen, run a tabpull subcommand in the terminal, come back.

        A child process, because Playwright's sync API refuses to start inside
        Textual's running asyncio loop.
        """
        files = ['--jobs', str(self.jobs_file), '--out', str(self.out_dir)]
        with self.suspend():
            print(f'\n$ {shlex.join(["tabpull", *argv])}', flush=True)
            try:
                code = subprocess.run(  # noqa: S603 - argv is our own subcommand
                    [sys.executable, '-m', 'tabpull', *files, *argv], check=False
                ).returncode
            except KeyboardInterrupt:
                code = 130
            with suppress(EOFError, KeyboardInterrupt):
                input('\nPress Enter to go back to tabpull. ')
        return code

    def run_jobs(self, names: list[str]) -> None:
        code = self.shell('run', '--', *names)
        label = ', '.join(names)
        if code == 0:
            self.last_run = f'last run: {label} ok'
            self.notify(f'Exported {label}')
        else:
            self.last_run = f'last run: {label} FAILED (exit {code})'
            self.notify(
                f'Run failed ({label}). The summary it printed names the failures.',
                severity='error',
                timeout=10,
            )


def run_tui(jobs_file: Path, out_dir: Path) -> None:
    TabpullApp(jobs_file, out_dir).run()
