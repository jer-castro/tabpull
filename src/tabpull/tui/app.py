import tomllib
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, override

import tableauserverclient as tsc
from playwright.sync_api import sync_playwright
from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Label, Static

from tabpull import ui
from tabpull.add import (
    MAX_SEARCH_RESULTS,
    ListedFilter,
    append_job,
    filter_defaults,
    listed_filters,
    read_view,
    search_views,
    view_from_flag,
    view_path,
)
from tabpull.embed import ViewInfo
from tabpull.filters import format_filter, resolved_filters, same_filter
from tabpull.home import site_rows, sso_badge
from tabpull.jobs import (
    Job,
    JobError,
    RangeFilter,
    ValuesFilter,
    check_known,
    delete_jobs,
    load_jobs,
    slug,
    update_job,
)
from tabpull.run import run_jobs as export_jobs
from tabpull.tableau import (
    MissingSettingsError,
    Settings,
    UnknownSiteError,
    browser_session,
    list_sites,
    load_site,
    parse_tableau_url,
)
from tabpull.tui.forms import (
    FORM_CSS,
    ChecksForm,
    ChoiceScreen,
    ConfirmScreen,
    FilterForm,
    ParamForm,
    PickScreen,
    SetupForm,
    SheetsForm,
    TaskScreen,
    TextForm,
    sign_in,
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
_MISSING_SHEET = ' (not on the live view)'


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


def _save_error(e: Exception) -> JobError:
    return e if isinstance(e, JobError) else JobError(str(e))


class HomeScreen(Screen[None]):
    app: 'TabpullApp'

    BINDINGS: ClassVar[list[BindingType]] = [
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

    @override
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
        else:
            self.notify('No jobs to run. Press a to add one.', severity='warning')

    def action_remove(self) -> None:
        names = self._selected()
        if not names:
            return

        def remove() -> None:
            try:
                delete_jobs(self.app.jobs_file, names)
            except (JobError, tomllib.TOMLDecodeError, OSError) as e:
                self.notify(str(e), severity='error')
            else:
                self.marked -= set(names)
                self.notify(f'Removed {", ".join(names)}')
            self.reload()

        self.app.push_screen(
            ConfirmScreen(f'Remove {", ".join(names)} from the jobs file?', remove)
        )

    def action_add(self) -> None:
        self.app.start_add()

    def action_setup(self) -> None:
        self.app.push_screen(SetupForm())

    def action_login(self) -> None:
        self.app.start_login()

    def action_reload(self) -> None:
        self.reload()


@dataclass
class _CachedView:
    key: tuple[str, str]
    info: ViewInfo


class JobScreen(Screen[None]):
    app: 'TabpullApp'

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding('escape', 'back', 'back'),
        Binding('enter,e', 'edit', 'edit', priority=True),
        Binding('n', 'new', 'new'),
        Binding('c', 'change', 'change field'),
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
        self._cached_view: _CachedView | None = None

    @override
    def compose(self) -> ComposeResult:
        yield Static('', id='summary', classes='info')
        yield Label(
            'Filters  (n new, c change field, e/enter edit, d delete)',
            classes='section',
        )
        yield DataTable(id='filters', cursor_type='row', zebra_stripes=True)
        yield Label(
            'Parameters  (n new, c change field, e/enter edit, d delete)',
            classes='section',
        )
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
                (item.field, item.sheet or f'({job.sheets[0]})', item.kind, item.shown)
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

    def _filter_form(
        self,
        index: int | None,
        preset: ValuesFilter | RangeFilter | None = None,
    ) -> None:
        current = None if index is None else self.job.filters[index]
        item = current if preset is None else preset

        def save(new: ValuesFilter | RangeFilter) -> None:
            if index is not None:
                self._reject_duplicate_filter(index, new)
            filters = list(self.job.filters)
            if index is None:
                filters.append(new)
            else:
                filters[index] = new
            self.commit(replace(self.job, filters=filters))

        if preset is not None:
            title = f'Filter {preset.field}'
        elif current is None:
            title = 'New filter'
        else:
            title = f'Edit filter {current.field}'
        self.app.push_screen(FilterForm(title, item, self.job.sheets[0], save))

    def _reject_duplicate_filter(
        self, index: int, new: ValuesFilter | RangeFilter
    ) -> None:
        default = self.job.sheets[0]
        original = self.job.filters[index]
        new_sheet = new.sheet or default
        same_row = new.field == original.field and new_sheet == (
            original.sheet or default
        )
        if same_row:
            return
        for other_index, other in enumerate(self.job.filters):
            if other_index == index:
                continue
            if other.field == new.field and (other.sheet or default) == new_sheet:
                msg = (
                    f'filter {new.field} on {new_sheet} is already on this job; '
                    'edit that row'
                )
                raise JobError(msg)

    def _param_form(
        self, index: int | None, preset: tuple[str, str] | None = None
    ) -> None:
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

        if preset is None:
            value = '' if old is None else self.job.params[old]
            name = old or ''
            title = 'New parameter' if old is None else f'Edit parameter {old}'
        else:
            name, value = preset
            title = name
        self.app.push_screen(ParamForm(title, name, value, save))

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
            self._request_catalog('param-new', None)
        else:
            self._request_catalog('filter-new', None)

    def action_change(self) -> None:
        table = self._focused_table()
        if table is None or (row := _row(table)) is None:
            return
        kind = 'param-change' if table.id == 'params' else 'filter-change'
        self._request_catalog(kind, row)

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

        def delete() -> None:
            try:
                self.commit(new)
            except JobError as e:
                self.notify(str(e), severity='error')

        self.app.push_screen(ConfirmScreen(f'Delete {label}?', delete))

    def action_sheets(self) -> None:
        self._request_catalog('sheets', None)

    def _save_sheets(self, sheets: list[str]) -> None:
        self.commit(replace(self.job, sheets=sheets))

    def _sheets_form(self) -> None:
        self.app.push_screen(
            SheetsForm(
                f'Sheets for {self.job.name}',
                list(self.job.sheets),
                self._save_sheets,
            )
        )

    def _request_catalog(self, kind: str, row: int | None) -> None:
        key = (self.job.site, self.job.view)
        cached = self._cached_view
        if cached is not None and cached.key == key:
            self._open_catalog(kind, row, cached.info)
            return
        self._read_catalog(kind, row, key)

    def _read_catalog(self, kind: str, row: int | None, key: tuple[str, str]) -> None:
        view = key[1]
        try:
            settings = load_site(key[0])
        except (UnknownSiteError, MissingSettingsError, ValueError) as exc:
            self.notify(str(exc), severity='error')
            self._typed_fallback(kind, row)
            return

        def work() -> ViewInfo:
            with sync_playwright() as playwright:
                return read_view(browser_session(playwright, settings), settings, view)

        def opened(info: ViewInfo) -> None:
            self._cached_view = _CachedView(key, info)
            self._open_catalog(kind, row, info)

        def failed(message: str) -> None:
            self.notify(message, severity='error', timeout=10)
            self._typed_fallback(kind, row)

        started = self.app.start_task(
            TaskScreen(
                f'Opening {view}…',
                work,
                opened,
                failed,
                on_cancel=lambda: self.notify('Cancelled.', severity='warning'),
            )
        )
        if not started:
            self._typed_fallback(kind, row)

    def _typed_fallback(self, kind: str, row: int | None) -> None:
        if kind == 'sheets':
            self._sheets_form()
        elif kind == 'filter-new':
            self._filter_form(None)
        elif kind == 'filter-change' and row is not None:
            self._filter_form(row)
        elif kind == 'param-new':
            self._param_form(None)
        elif kind == 'param-change' and row is not None:
            self._param_form(row)

    def _open_catalog(self, kind: str, row: int | None, info: ViewInfo) -> None:
        if kind == 'sheets':
            self._open_sheet_catalog(info)
        elif kind.startswith('filter'):
            self._open_filter_catalog(kind, row, info)
        else:
            self._open_param_catalog(kind, row, info)

    def _is_filter(
        self, raw: ListedFilter, existing: ValuesFilter | RangeFilter
    ) -> bool:
        probe = ValuesFilter(raw['field'], [], raw['sheet'])
        return same_filter(probe, existing, self.job.sheets[0])

    def _matching_filter(self, raw: ListedFilter) -> int | None:
        return next(
            (i for i, f in enumerate(self.job.filters) if self._is_filter(raw, f)),
            None,
        )

    def _filter_options(
        self, listed: list[ListedFilter], *, skip: int | None
    ) -> list[tuple[str, str]]:
        options: list[tuple[str, str]] = []
        for index, raw in enumerate(listed):
            if skip is not None and self._is_filter(raw, self.job.filters[skip]):
                continue
            match = self._matching_filter(raw)
            label = f'{raw["field"]}  on {raw["sheet"]}'
            if match is not None:
                saved = self.job.filters[match]
                label = f'{_MARK} {label}  = {saved.shown}'
            options.append((label, str(index)))
        return options

    def _open_filter_catalog(self, kind: str, row: int | None, info: ViewInfo) -> None:
        listed = listed_filters(info['sheets'], self.job.sheets)
        if kind == 'filter-new':
            self.app.push_screen(
                PickScreen(
                    'Add a filter',
                    self._filter_options(listed, skip=None),
                    lambda value: self._choose_new_filter(listed, value),
                    lambda: None,
                )
            )
            return
        if row is None:
            return
        self.app.push_screen(
            ChoiceScreen(
                f'Change field of {self.job.filters[row].field}',
                self._filter_options(listed, skip=row),
                lambda value: self._choose_changed_filter(listed, row, value),
            )
        )

    def _choose_new_filter(self, listed: list[ListedFilter], value: str) -> None:
        raw = listed[int(value)]
        match = self._matching_filter(raw)
        if match is None:
            self._filter_form(None, filter_defaults(raw))
            return
        self._filter_form(match)

    def _choose_changed_filter(
        self, listed: list[ListedFilter], row: int, value: str
    ) -> None:
        self._filter_form(row, filter_defaults(listed[int(value)]))

    def _param_label(self, name: str) -> str:
        if name in self.job.params:
            return f'{_MARK} {name}  = {self.job.params[name]}'
        return name

    def _param_options(
        self, params: list[dict[str, str]], *, skip: str | None
    ) -> list[tuple[str, str]]:
        return [
            (self._param_label(raw['name']), str(index))
            for index, raw in enumerate(params)
            if raw['name'] != skip
        ]

    def _open_param_catalog(self, kind: str, row: int | None, info: ViewInfo) -> None:
        params = info['params']
        if kind == 'param-new':
            self.app.push_screen(
                PickScreen(
                    'Set a parameter',
                    self._param_options(params, skip=None),
                    lambda value: self._choose_new_param(params, value),
                    lambda: None,
                )
            )
            return
        if row is None:
            return
        current = list(self.job.params)[row]
        self.app.push_screen(
            ChoiceScreen(
                f'Change field of {current}',
                self._param_options(params, skip=current),
                lambda value: self._choose_changed_param(params, row, value),
            )
        )

    def _choose_new_param(self, params: list[dict[str, str]], value: str) -> None:
        raw = params[int(value)]
        names = list(self.job.params)
        if raw['name'] in self.job.params:
            self._param_form(names.index(raw['name']))
            return
        self._param_form(None, (raw['name'], raw['current']))

    def _choose_changed_param(
        self, params: list[dict[str, str]], row: int, value: str
    ) -> None:
        raw = params[int(value)]
        self._param_form(row, (raw['name'], raw['current']))

    def _open_sheet_catalog(self, info: ViewInfo) -> None:
        live = [sheet['name'] for sheet in info['sheets']]
        present = set(live)
        saved = list(self.job.sheets)
        options = [*saved, *[name for name in live if name not in set(saved)]]
        suffix = {name: _MISSING_SHEET for name in saved if name not in present}
        self.app.push_screen(
            ChecksForm(
                f'Sheets for {self.job.name}',
                options,
                self._save_sheets,
                ticked=set(saved),
                label_suffix=suffix,
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
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding('q', 'quit', 'quit'),
        Binding('question_mark', 'help', 'help'),
    ]

    def __init__(self, jobs_file: Path, out_dir: Path) -> None:
        super().__init__()
        self.jobs_file = jobs_file
        self.out_dir = out_dir
        self.last_run = ''
        self._tasks: set[int] = set()
        self._quit_armed = False

    def on_mount(self) -> None:
        self.push_screen(HomeScreen())

    async def action_quit(self) -> None:
        if self._tasks and not self._quit_armed:
            self._quit_armed = True
            if isinstance(self.screen, TaskScreen):
                self.screen.action_cancel()
            else:
                for worker in self.workers:
                    if worker.group == 'tabpull-task':
                        worker.cancel()
            self.notify('Stopping… (q again to force)', severity='warning')
            return
        self.exit()

    def hold_task(self, key: int) -> None:
        self._tasks.add(key)

    def release_task(self, key: int) -> None:
        self._tasks.discard(key)

    def start_task[T](self, screen: TaskScreen[T]) -> bool:
        if self._tasks:
            self.notify('Still stopping the previous task…', severity='warning')
            return False
        self.push_screen(screen)
        return True

    def action_help(self) -> None:
        if self.screen.query('HelpPanel'):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()

    def run_jobs(self, names: list[str]) -> None:
        def work() -> str:
            if ui.stopped():
                msg = 'Cancelled.'
                raise SystemExit(msg)
            jobs = load_jobs(self.jobs_file)
            check_known(names, jobs)
            wanted = set(names)
            selected = [job for job in jobs if job.name in wanted]
            ui.emit(f'Exporting {len(selected)} job(s) to {self.out_dir}/')
            failed = export_jobs(selected, self.out_dir, _TuiReport())
            if ui.stopped():
                msg = 'Cancelled.'
                raise SystemExit(msg)
            if failed:
                msg = f'{len(failed)} failed: {", ".join(failed)}'
                raise SystemExit(msg)
            return f'Exported {", ".join(job.name for job in selected)}'

        self.start_task(
            TaskScreen(
                'Export',
                work,
                lambda summary: self._finish_run(names, summary, failed=False),
                lambda message: self._finish_run(names, message, failed=True),
                on_cancel=lambda: self._finish_run(
                    names, 'Cancelled.', failed=False, cancelled=True
                ),
                hold_failure=True,
            )
        )

    def _finish_run(
        self, names: list[str], message: str, *, failed: bool, cancelled: bool = False
    ) -> None:
        label = ', '.join(names)
        if cancelled:
            self.last_run = f'last run: {label} cancelled'
            self.notify('Cancelled.', severity='warning')
        elif failed:
            self.last_run = f'last run: {label} FAILED ({message})'
            self.notify(message, severity='error', timeout=10)
        else:
            self.last_run = f'last run: {label} ok'
            self.notify(message)
        if isinstance(self.screen, HomeScreen):
            self.screen.reload()

    def start_login(self) -> None:
        names = list_sites()
        if not names:
            self.notify(
                'No Tableau site configured. Press s to set up a site.',
                severity='warning',
            )
            return
        if len(names) == 1:
            self._login(names[0])
            return
        self.push_screen(
            ChoiceScreen('Site', [(name, name) for name in names], self._login)
        )

    def _login(self, name: str) -> None:
        try:
            settings = load_site(name)
        except (UnknownSiteError, MissingSettingsError, ValueError) as e:
            self.notify(str(e), severity='error')
            return

        self.start_task(
            TaskScreen(
                f'Signing in to {name}…',
                lambda: sign_in(settings),
                lambda _name: self._login_done(name),
                lambda message: self.notify(message, severity='error', timeout=10),
            )
        )

    def _login_done(self, name: str) -> None:
        self.notify(f'Signed in to {name}')
        if isinstance(self.screen, HomeScreen):
            self.screen.reload()

    def start_add(self) -> None:
        names = list_sites()
        if not names:
            self.notify('No Tableau site configured. Starting setup.')
            self.push_screen(SetupForm(self._add_site))
            return
        if len(names) == 1:
            self._add_site(names[0])
            return
        self.push_screen(
            ChoiceScreen('Site', [(name, name) for name in names], self._add_site)
        )

    def _add_site(self, name: str) -> None:
        try:
            settings = load_site(name)
        except (UnknownSiteError, MissingSettingsError, ValueError) as e:
            self.notify(str(e), severity='error')
            return
        self.push_screen(
            TextForm(
                'Add job',
                'View URL or part of a workbook/view name',
                '',
                lambda query: self._queue_search(settings, query),
            )
        )

    def _queue_search(self, settings: Settings, query: str) -> None:
        self.call_later(self._search, settings, query)

    def _search(self, settings: Settings, query: str) -> None:
        def failed(message: str) -> None:
            self.notify(message, severity='error', timeout=10)

        self.start_task(
            TaskScreen(
                'Searching views…',
                lambda: search_views(settings, query),
                lambda matches: self._views(settings, query, matches),
                failed,
            )
        )

    def _views(
        self, settings: Settings, query: str, matches: list[tsc.ViewItem]
    ) -> None:
        if not matches:
            self.notify(
                f'No views you can access match {query!r}.', severity='error', timeout=8
            )
            return
        target = parse_tableau_url(query).view if query.startswith('http') else None
        if target and len(matches) == 1:
            self._name_job(settings, matches[0])
            return
        shown = matches[:MAX_SEARCH_RESULTS]
        if len(matches) > MAX_SEARCH_RESULTS:
            self.notify(
                f'Showing the best {MAX_SEARCH_RESULTS} of {len(matches)} matches; '
                'type more of the name to narrow it.'
            )
        disabled: set[str] = set()
        options: list[tuple[str, str]] = []
        for index, item in enumerate(shown):
            kind = item.sheet_type or 'view'
            options.append((f'{item.name}  {item.content_url}  {kind}', str(index)))
            if kind.lower() == 'story':
                disabled.add(str(index))
        self.push_screen(
            ChoiceScreen(
                'View',
                options,
                lambda index: self._name_job(settings, shown[int(index)]),
                disabled=disabled,
            )
        )

    def _name_job(self, settings: Settings, item: tsc.ViewItem) -> None:
        view = view_path(item)
        try:
            existing = {job.name for job in load_jobs(self.jobs_file)}
        except (JobError, tomllib.TOMLDecodeError, OSError) as e:
            self.notify(str(e), severity='error')
            return
        default = slug(item.name or view)

        def save(name: str) -> None:
            if name in existing:
                msg = f'A job named {name!r} already exists in {self.jobs_file}.'
                raise JobError(msg)
            self.call_later(self._inspect, settings, name, view)

        self.push_screen(
            TextForm(
                'Job name',
                'Job name (exports go to <out>/<name>/)',
                default,
                save,
            )
        )

    def _inspect(self, settings: Settings, name: str, view: str) -> None:
        def work() -> ViewInfo:
            with sync_playwright() as playwright:
                return read_view(browser_session(playwright, settings), settings, view)

        def opened(info: ViewInfo) -> None:
            draft = _Add(settings, view, name, info)
            names = [sheet['name'] for sheet in info['sheets']]

            def chosen(sheets: list[str]) -> None:
                draft.sheets = sheets
                draft.listed = listed_filters(info['sheets'], sheets)
                self.call_later(self._pick_filters, draft)

            self.push_screen(ChecksForm('Sheets to crosstab', names, chosen))

        self.start_task(
            TaskScreen(
                f'Opening {view}…',
                work,
                opened,
                lambda message: self.notify(message, severity='error', timeout=10),
            )
        )

    def _pick_filters(self, draft: '_Add') -> None:
        if not draft.listed:
            self._params(draft)
            return
        options = [
            (f'{item["field"]}  on {item["sheet"]}', str(index))
            for index, item in enumerate(draft.listed)
        ]
        self.push_screen(
            PickScreen(
                'Add a filter',
                options,
                lambda index: self._add_filter(draft, draft.listed[int(index)]),
                lambda: self._params(draft),
            )
        )

    def _add_filter(self, draft: '_Add', raw: ListedFilter) -> None:
        self.push_screen(
            FilterForm(
                f'Filter {raw["field"]}',
                filter_defaults(raw),
                draft.sheets[0],
                draft.filters.append,
            )
        )

    def _params(self, draft: '_Add') -> None:
        if not draft.raw_params:
            self._save_add(draft)
            return
        options = [
            (item['name'], str(index)) for index, item in enumerate(draft.raw_params)
        ]

        def pick(index: str) -> None:
            raw = draft.raw_params[int(index)]

            def save(pair: tuple[str, str]) -> None:
                draft.params[pair[0]] = pair[1]

            self.push_screen(ParamForm(raw['name'], raw['name'], raw['current'], save))

        self.push_screen(
            PickScreen('Set a parameter', options, pick, lambda: self._save_add(draft))
        )

    def _save_add(self, draft: '_Add') -> None:
        job = Job(
            draft.name,
            draft.view,
            draft.sheets,
            draft.settings.name,
            list(draft.filters),
            dict(draft.params),
        )
        try:
            append_job(
                self.jobs_file,
                replace(job, filters=resolved_filters(job)),
                quiet=True,
            )
        except (JobError, OSError) as e:
            self.notify(str(e), severity='error')
            return
        self.notify(f'Added {draft.name}')


@dataclass
class _Add:
    settings: Settings
    view: str
    name: str
    info: ViewInfo
    sheets: list[str] = field(default_factory=list)
    filters: list[ValuesFilter | RangeFilter] = field(default_factory=list)
    params: dict[str, str] = field(default_factory=dict)
    listed: list[ListedFilter] = field(default_factory=list)

    @property
    def raw_params(self) -> list[dict[str, str]]:
        return self.info['params']


class _TuiReport:
    def live(self) -> AbstractContextManager[object]:
        return nullcontext()

    def sheet(self, job: Job, done: int, sheet: str) -> None:
        ui.emit(f'{job.name}: exporting {sheet} ({done + 1}/{len(job.sheets)})')

    def ok(self, job: Job, paths: Sequence[Path]) -> None:
        ui.emit(f'✓ {job.name}: {", ".join(map(str, paths))}')

    def fail(self, job: Job, message: str) -> None:
        ui.emit(f'✗ {job.name}: {message.partition("\n")[0]}')

    def summary(self, ok: int, total: int, rerun: str | None) -> None:  # noqa: ARG002
        return None


def run_tui(jobs_file: Path, out_dir: Path) -> None:
    TabpullApp(jobs_file, out_dir).run()
