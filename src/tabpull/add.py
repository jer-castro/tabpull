import argparse
import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import TypedDict

import questionary
import tableauserverclient as tsc
from playwright.sync_api import BrowserContext, sync_playwright
from rich import box
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from tabpull import ui
from tabpull.embed import INSPECT_JS, SheetInfo, ViewInfo, open_view
from tabpull.filters import (
    format_filter,
    normalize_range_bound,
    parse_filter_spec,
    parse_param_spec,
    resolved_filters,
    split_values,
)
from tabpull.jobs import (
    Job,
    JobError,
    RangeFilter,
    ValuesFilter,
    job_to_toml,
    load_jobs,
    slug,
)
from tabpull.tableau import (
    Settings,
    browser_session,
    parse_tableau_url,
    rest_session,
)

MAX_SEARCH_RESULTS = 30
MIN_MATCH_SCORE = 0.75
_WORD_RE = re.compile(r'[^\W_]+')


def _view_path(item: tsc.ViewItem) -> str:
    return (item.content_url or '').replace('/sheets/', '/', 1)


def match_score(query: str, text: str) -> float:
    words = _WORD_RE.findall(text.lower())
    terms = _WORD_RE.findall(query.lower())
    if not words or not terms:
        return 0.0
    return min(
        max(1.0 if t in w else SequenceMatcher(None, t, w).ratio() for w in words)
        for t in terms
    )


class _ListedFilter(TypedDict):
    field: str
    sheet: str
    type: str
    current: str


def _validate_range_answer(value: str) -> bool | str:
    try:
        normalize_range_bound(value.strip() or None)
    except JobError as e:
        return str(e)
    return True


def _prompt_text(
    message: str,
    *,
    default: str = '',
    validate: Callable[[str], bool | str] | None = None,
) -> str:
    return ui.ask(
        questionary.text(message, default=default, validate=validate, style=ui.STYLE)
    )


def _prompt_bound(message: str, default: str) -> str | None:
    answer = _prompt_text(message, default=default, validate=_validate_range_answer)
    bound = answer.strip() or None
    normalize_range_bound(bound)
    return bound


def _find_view(settings: Settings) -> tsc.ViewItem:
    query = _prompt_text('View URL or part of a workbook/view name').strip()
    with ui.console.status('Searching views…'), rest_session(settings) as server:
        options = tsc.RequestOptions(pagesize=1000)
        options.fields |= {'_default_', 'sheetType'}
        views = list(tsc.Pager(server.views, options))
    target = parse_tableau_url(query).view if query.startswith('http') else None
    if target:
        matches = [item for item in views if _view_path(item) == target]
    else:
        scored = [
            (match_score(query, f'{item.name} {item.content_url}'), item)
            for item in views
        ]
        scored.sort(key=lambda pair: -pair[0])
        matches = [item for score, item in scored if score >= MIN_MATCH_SCORE]
    if not matches:
        msg = f'No views you can access match {query!r}.'
        raise SystemExit(msg)
    if target and len(matches) == 1:
        return matches[0]
    if len(matches) > MAX_SEARCH_RESULTS:
        print(
            f'  Showing the best {MAX_SEARCH_RESULTS} of {len(matches)} matches; type more of the name to narrow it.'
        )
        matches = matches[:MAX_SEARCH_RESULTS]
    choices = []
    for item in matches:
        kind = item.sheet_type or 'view'
        choices.append(
            questionary.Choice(
                f'{item.name}  {item.content_url}  {kind}',
                value=item,
                disabled='stories unsupported' if kind.lower() == 'story' else None,
            )
        )
    return ui.ask(
        questionary.select(
            'View',
            choices=choices,
            use_search_filter=True,
            use_jk_keys=False,
            instruction='(type to filter)',
            style=ui.STYLE,
        )
    )


def _print_fields(
    sheets: list[SheetInfo], chosen: Sequence[str], params: list[dict[str, str]]
) -> list[_ListedFilter]:
    table = Table(
        title='Filters & parameters on these sheets',
        box=box.ROUNDED,
        title_justify='left',
    )
    for column in ('kind', 'name', 'type', 'sheet', 'current'):
        table.add_column(column)
    picked_names = set(chosen)
    listed: list[_ListedFilter] = []
    for sheet in sheets:
        if sheet['name'] not in picked_names:
            continue
        for item in sheet['filters']:
            row: _ListedFilter = {
                'field': item['field'],
                'sheet': sheet['name'],
                'type': item['type'],
                'current': item['current'],
            }
            listed.append(row)
            table.add_row(
                'filter', row['field'], row['type'], row['sheet'], row['current']
            )
    for param in params:
        table.add_row('param', param['name'], '', '', param['current'])
    ui.console.print(table)
    return listed


def _prompt_filters(listed: list[_ListedFilter]) -> list[ValuesFilter | RangeFilter]:
    filters: list[ValuesFilter | RangeFilter] = []
    if not listed:
        return filters
    while True:
        choices = [
            questionary.Choice(f'{item["field"]}  on {item["sheet"]!r}', value=item)
            for item in listed
        ]
        item = ui.ask(
            questionary.select(
                'Add a filter', choices=[*choices, 'Done'], style=ui.STYLE
            )
        )
        if item == 'Done':
            return filters
        field_name, sheet = item['field'], item['sheet']
        if item['type'] == 'range':
            low, sep, high = item['current'].partition(' .. ')
            if not sep:
                low = high = ''
            while True:
                start = _prompt_bound(f'{field_name} from', low)
                end = _prompt_bound(f'{field_name} to', high)
                if start or end:
                    break
                ui.console.print('Give a from, a to, or both.')
            filters.append(RangeFilter(field_name, sheet, start, end))
        else:
            default = '' if item['current'] == '(All)' else item['current']
            values = _prompt_text(f'{field_name} values (a|b)', default=default)
            filters.append(ValuesFilter(field_name, split_values(values), sheet))


def _prompt_params(params: list[dict[str, str]]) -> dict[str, str]:
    chosen: dict[str, str] = {}
    if not params:
        return chosen
    while True:
        choices = [questionary.Choice(item['name'], value=item) for item in params]
        param = ui.ask(
            questionary.select(
                'Set a parameter', choices=[*choices, 'Done'], style=ui.STYLE
            )
        )
        if param == 'Done':
            return chosen
        chosen[param['name']] = _prompt_text(param['name'], default=param['current'])


def _build_embed_job(
    context: BrowserContext, settings: Settings, name: str, view: str
) -> Job:
    with ui.console.status(f'Opening {view} in headless browser…'):
        page = open_view(context, settings, view)
        info: ViewInfo = page.evaluate(INSPECT_JS)
        page.close()
    chosen: list[str] = ui.ask(
        questionary.checkbox(
            'Sheets to crosstab',
            choices=[sheet['name'] for sheet in info['sheets']],
            validate=lambda picked: bool(picked) or 'Pick at least one sheet',
            style=ui.STYLE,
        )
    )
    listed = _print_fields(info['sheets'], chosen, info['params'])
    job = Job(
        name,
        view,
        chosen,
        settings.name,
        _prompt_filters(listed),
        _prompt_params(info['params']),
    )
    return replace(job, filters=resolved_filters(job))


def _using_site(settings: Settings) -> None:
    detail = (
        f'Using site {settings.name!r} ({settings.server}, '
        f'site {settings.site or "(default)"}).'
    )
    ui.console.print(f'[dim]{escape(detail)}[/]', soft_wrap=True)


def append_job(path: Path, job: Job) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = job_to_toml(job)
    prefix = '\n' if path.exists() and path.stat().st_size else ''
    with path.open('a', encoding='utf-8') as handle:
        handle.write(prefix + text)
    if not ui.rich_output():
        print(f'\nSaved job {job.name!r} to {path}:\n\n{text}')
        print(f'Run it: tabpull run {job.name}')
        return
    filters = '; '.join(format_filter(item) for item in job.filters) or '-'
    params = '; '.join(f'{key}={value}' for key, value in job.params.items()) or '-'
    body = '\n'.join(
        (
            f'[bold]{escape(job.name)}[/]  on {escape(job.site)}',
            f'view    {escape(job.view)}',
            f'sheets  {escape(", ".join(job.sheets))}',
            f'filters {escape(filters)}',
            f'params  {escape(params)}',
            escape(str(path)),
        )
    )
    ui.console.print(
        Panel(
            body,
            title='[green]✓ Saved[/]',
            title_align='left',
            border_style='green',
            subtitle=f'run it: [bold]tabpull run {escape(job.name)}[/]',
            subtitle_align='left',
        )
    )


def add_job(settings: Settings, jobs_path: Path, name: str | None = None) -> None:
    _using_site(settings)
    item = _find_view(settings)
    view = _view_path(item)
    existing = {job.name for job in load_jobs(jobs_path)}
    default_name = slug(item.name or view)
    if name is None:

        def reject_existing(value: str) -> bool | str:
            candidate = value.strip() or default_name
            if candidate in existing:
                return f'A job named {candidate!r} already exists in {jobs_path}.'
            return True

        name = (
            _prompt_text(
                'Job name', default=default_name, validate=reject_existing
            ).strip()
            or default_name
        )
    if name in existing:
        msg = f'A job named {name!r} already exists in {jobs_path}.'
        raise SystemExit(msg)

    with sync_playwright() as pw:
        job = _build_embed_job(browser_session(pw, settings), settings, name, view)
    append_job(jobs_path, job)


def view_from_flag(value: str) -> str:
    text = value.strip()
    if text.startswith(('http://', 'https://')):
        parsed = parse_tableau_url(text)
        if not parsed.view:
            msg = f'{text!r} has no view. Use Workbook/View or a view URL.'
            raise JobError(msg)
        return parsed.view
    workbook, sep, view = text.partition('/')
    if not sep or not workbook or not view or '/' in view:
        msg = f'{text!r} should look like Workbook/View.'
        raise JobError(msg)
    return f'{workbook}/{view}'


def add_job_from_flags(
    settings: Settings, args: argparse.Namespace, jobs_file: Path
) -> None:
    view = view_from_flag(args.view)
    filters = [parse_filter_spec(spec) for spec in args.filter_specs or []]
    params = dict(parse_param_spec(spec) for spec in args.params or [])
    name = args.name or slug(view)
    if any(job.name == name for job in load_jobs(jobs_file)):
        msg = f'A job named {name!r} already exists in {jobs_file}.'
        raise SystemExit(msg)
    _using_site(settings)
    with sync_playwright() as pw:
        page = open_view(browser_session(pw, settings), settings, view)
        page.close()
    job = Job(name, view, list(args.sheets), settings.name, filters, params)
    append_job(jobs_file, replace(job, filters=resolved_filters(job)))
