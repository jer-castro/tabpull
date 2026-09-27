"""Export Tableau crosstab CSVs for the dashboard sheets you name.

Commands:
  setup  save a site's personal access token and SSO session
  add    record a job (prompts, or flags for the site, view, sheets, and filters)
  run    export jobs from the jobs file into the current folder
  login  refresh a site's SSO session

Run tabpull with no command to see configured sites and saved jobs.
"""

import argparse
import codecs
import csv
import io
import json
import os
import re
import shlex
import string
import sys
import tomllib
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from difflib import SequenceMatcher
from functools import partial
from pathlib import Path
from typing import Any, NoReturn, TypedDict

import questionary
import tableauserverclient as tsc
from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
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

from tabpull import ui, wizard
from tabpull.cli import VERSION_FLAGS, version
from tabpull.tableau import (
    MissingSettingsError,
    Settings,
    UnknownSiteError,
    browser_session,
    check_site_name,
    jobs_path,
    list_sites,
    load_site,
    parse_tableau_url,
    rest_session,
    sso_login,
)

VIZ_LOAD_TIMEOUT_MS = 180_000
DOWNLOAD_TIMEOUT_MS = 300_000
MAX_SEARCH_RESULTS = 30
MIN_MATCH_SCORE = 0.75
_WORD_RE = re.compile(r'[^\W_]+')
# Tableau's range-filter API only accepts a Date or a number. M/D/YYYY is what the
# dashboard shows for a date filter; YYYY-MM-DD is what the embedding call converts.
# Anything else on a range bound is a relative date or a date computed at run time.
_US_DATE = re.compile(r'^(\d{1,2})/(\d{1,2})/(\d{4})$')
_ISO_DATE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')
_NUMBER = re.compile(r'^-?\d+(\.\d+)?$')
# A fake page on the Tableau origin: the embedded viz then loads first-party, so the SSO
# cookies apply and "restrict embedding to these domains" site settings can't block it.
HOST_PATH = '/__crosstab_host__'

HOST_PAGE = string.Template("""<!doctype html>
<meta charset="utf-8">
<body>
<script type="module">
try {
  const api = await import($script_url);
  window.tableauEnums = { CrosstabFileFormat: api.CrosstabFileFormat, FilterDomainType: api.FilterDomainType, FilterUpdateType: api.FilterUpdateType };
  const viz = new api.TableauViz();
  viz.src = $view_url;
  viz.toolbar = 'hidden';
  viz.hideTabs = true;
  viz.addEventListener(api.TableauEventType.FirstInteractive, () => { window.vizState = 'ready'; });
  viz.addEventListener(api.TableauEventType.VizLoadError, (e) => {
    window.vizState = 'error: ' + (e.detail?.message ?? JSON.stringify(e.detail));
  });
  window.viz = viz;
  document.body.appendChild(viz);
} catch (e) {
  window.vizState = 'error: could not load the Tableau Embedding API: ' + e;
}
</script>
""")

STORY_REFUSAL = 'Stories are not supported; use the dashboard inside it.'
_STORY_GUARD_JS = (
    "if (window.viz.workbook.activeSheet.sheetType === 'story') "
    'throw new Error(' + json.dumps(STORY_REFUSAL) + ');'
)

STORY_JS = '() => {\n  ' + _STORY_GUARD_JS + '\n}'

INSPECT_JS = (
    'async () => {\n  '
    + _STORY_GUARD_JS
    + """
  const active = window.viz.workbook.activeSheet;
  const worksheets = active.sheetType === 'worksheet' ? [active] : active.worksheets;
  const current = (f) => {
    if (f.filterType === 'categorical') return f.isAllSelected ? '(All)' : f.appliedValues.map((v) => v.formattedValue).join(' | ');
    if (f.filterType === 'range') return (f.minValue?.formattedValue ?? '') + ' .. ' + (f.maxValue?.formattedValue ?? '');
    return '';
  };
  const sheets = [];
  for (const ws of worksheets) {
    const filters = await ws.getFiltersAsync();
    sheets.push({ name: ws.name, filters: filters.map((f) => ({ field: f.fieldName, type: f.filterType, current: current(f) })) });
  }
  const params = await window.viz.workbook.getParametersAsync();
  return { sheets, params: params.map((p) => ({ name: p.name, current: p.currentValue.formattedValue })) };
}"""
)

APPLY_JS = """async ({ filters, params }) => {
  const viz = window.viz;
  const active = viz.workbook.activeSheet;
  const worksheets = active.sheetType === 'worksheet' ? [active] : active.worksheets;
  const toValue = (v) => {
    if (typeof v !== 'string') return v;
    if (v.trim() === '') return v;
    // Tableau reads range-filter Dates as UTC calendar days, whatever the browser timezone.
    // An impossible ISO date must not be returned: `new Date` shifts it (2024-02-31 -> March 2).
    const iso = /^(\\d{4})-(\\d{2})-(\\d{2})$/.exec(v);
    if (iso) {
      const year = Number(iso[1]);
      const month = Number(iso[2]);
      const day = Number(iso[3]);
      const parsed = new Date(v + 'T00:00:00Z');
      if (
        Number.isNaN(parsed.getTime()) ||
        parsed.getUTCFullYear() !== year ||
        parsed.getUTCMonth() !== month - 1 ||
        parsed.getUTCDate() !== day
      ) throw new Error(v + ' is not a real date; use YYYY-MM-DD or M/D/YYYY');
      return parsed;
    }
    if (/^-?\\d+(\\.\\d+)?$/.test(v)) return Number(v);
    throw new Error(v + ' is a relative date or a date computed at run time; use YYYY-MM-DD or M/D/YYYY');
  };
  // applyRangeFilterAsync refuses a null bound, so an open side takes the filter's own endpoint.
  const domainOf = async (ws, f) => {
    const found = (await ws.getFiltersAsync()).find((x) => x.fieldName === f.field && x.filterType === 'range');
    if (!found) throw new Error('No range filter on ' + f.field + ' in ' + f.sheet + ' to take an open end from; give both min and max');
    return found.getDomainAsync(window.tableauEnums.FilterDomainType.Database);
  };
  for (const [name, value] of Object.entries(params)) await viz.workbook.changeParameterValueAsync(name, value);
  for (const f of filters) {
    const ws = worksheets.find((w) => w.name === f.sheet);
    if (!ws) throw new Error('Sheet not in this view: ' + f.sheet);
    if (f.values) {
      await ws.applyFilterAsync(f.field, f.values, window.tableauEnums.FilterUpdateType.Replace);
      continue;
    }
    const domain = f.min == null || f.max == null ? await domainOf(ws, f) : null;
    await ws.applyRangeFilterAsync(f.field, {
      min: f.min == null ? domain.min.value : toValue(f.min),
      max: f.max == null ? domain.max.value : toValue(f.max),
    });
  }
}"""

EXPORT_JS = '(sheet) => window.viz.exportCrosstabAsync(sheet, window.tableauEnums.CrosstabFileFormat.CSV)'


@dataclass(frozen=True)
class ValuesFilter:
    field: str
    values: list[str]
    sheet: str = ''


@dataclass(frozen=True)
class RangeFilter:
    field: str
    sheet: str
    min: str | None = None
    max: str | None = None


@dataclass(frozen=True)
class Job:
    """Crosstab of dashboard sheets through the Embedding API, like Download > Crosstab."""

    name: str
    view: str
    sheets: list[str]
    site: str
    filters: list[ValuesFilter | RangeFilter] = field(default_factory=list)
    params: dict[str, str] = field(default_factory=dict)


class JobError(Exception):
    pass


class SheetInfo(TypedDict):
    name: str
    filters: list[dict[str, str]]


class ViewInfo(TypedDict):
    sheets: list[SheetInfo]
    params: list[dict[str, str]]


def _parse_filter(raw: object) -> ValuesFilter | RangeFilter:
    if not isinstance(raw, dict):
        msg = f'filter {raw!r} is not a table'
        raise JobError(msg)
    raw = dict(raw)
    raw['sheet'] = str(raw.get('sheet') or '').strip()
    try:
        return ValuesFilter(**raw) if 'values' in raw else RangeFilter(**raw)
    except TypeError as e:
        msg = str(e)
        raise JobError(msg) from e


def parse_job(raw: object) -> Job:
    if not isinstance(raw, dict):
        msg = f'job {raw!r} is not a table'
        raise JobError(msg)
    raw = dict(raw)
    name = raw.get('name', '?')
    if not raw.get('sheets'):
        msg = f'job {name!r}: needs at least one sheet'
        raise JobError(msg)
    site = raw.get('site')
    if not isinstance(site, str) or not site.strip():
        msg = f'job {name!r}: needs a site'
        raise JobError(msg)
    raw['site'] = site.strip()
    try:
        filters = [_parse_filter(item) for item in raw.pop('filters', [])]
        return Job(**raw, filters=filters)
    except (TypeError, KeyError, JobError) as e:
        msg = f'job {raw.get("name", "?")!r}: {e}'
        raise JobError(msg) from e


def load_jobs(path: Path) -> list[Job]:
    if not path.exists():
        return []
    raw_jobs = tomllib.loads(path.read_text(encoding='utf-8')).get('job', [])
    if not isinstance(raw_jobs, list):
        msg = '`job` must be an array of tables'
        raise JobError(msg)
    return [parse_job(raw) for raw in raw_jobs]


def _toml(value: object) -> str:
    if isinstance(value, list):
        return '[' + ', '.join(_toml(v) for v in value) + ']'
    if isinstance(value, dict):
        return (
            '{ '
            + ', '.join(f'{json.dumps(k)} = {_toml(v)}' for k, v in value.items())
            + ' }'
        )
    return json.dumps(value, ensure_ascii=False)


def job_to_toml(job: Job) -> str:
    fields = asdict(job)
    fields['filters'] = [
        {k: v for k, v in item.items() if v or isinstance(v, list)}
        for item in fields['filters']
    ]
    lines = [
        '[[job]]',
        f'name = {_toml(fields.pop("name"))}',
        f'site = {_toml(fields.pop("site"))}',
    ]
    lines += [f'{key} = {_toml(value)}' for key, value in fields.items() if value]
    return '\n'.join(lines) + '\n'


def resolved_filters(job: Job) -> list[ValuesFilter | RangeFilter]:
    """Apply a filter that names no sheet on the first sheet, and say so.

    The filter is not copied onto the other sheets. A sheet named on the filter is left alone.
    """
    if not job.sheets:
        msg = f'job {job.name!r}: needs at least one sheet'
        raise JobError(msg)
    default = job.sheets[0]
    resolved: list[ValuesFilter | RangeFilter] = []
    for item in job.filters:
        if item.sheet:
            resolved.append(item)
            continue
        _note_default_sheet(job, item.field, default)
        resolved.append(replace(item, sheet=default))
    return resolved


def _note_default_sheet(job: Job, field_name: str, sheet: str) -> None:
    print(
        f'  {job.name}: filter {field_name!r} names no sheet; '
        f'applying it on {sheet!r}, the first sheet in the job.'
    )


def _overrides(
    item: ValuesFilter | RangeFilter,
    existing: ValuesFilter | RangeFilter,
    default_sheet: str,
) -> bool:
    return existing.field == item.field and (
        not item.sheet or (existing.sheet or default_sheet) == item.sheet
    )


def filters_for_run(job: Job, overrides: Sequence[ValuesFilter | RangeFilter]) -> Job:
    """Use `overrides` for this run instead of the saved filters on the same field.

    The same overrides apply to every selected job. An override with ` @Sheet`
    replaces that sheet's filter on the field; one with no sheet replaces the
    field's filter on every sheet. A field the job does not filter there is added,
    on the first sheet when no sheet is named, and that is printed. Nothing is
    written to the jobs file.
    """
    if not overrides:
        return job
    if not job.sheets:
        msg = f'job {job.name!r}: needs at least one sheet'
        raise JobError(msg)
    default = job.sheets[0]
    filters: list[ValuesFilter | RangeFilter] = list(job.filters)
    for item in overrides:
        if any(_overrides(item, existing, default) for existing in filters):
            filters = [
                replace(item, sheet=item.sheet or existing.sheet)
                if _overrides(item, existing, default)
                else existing
                for existing in filters
            ]
            continue
        if not item.sheet:
            _note_default_sheet(job, item.field, default)
        filters.append(replace(item, sheet=item.sheet or default))
    return replace(job, filters=filters)


def _calendar_day(year: int, month: int, day: int, original: str) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        msg = f'{original!r} is not a real date; use YYYY-MM-DD or M/D/YYYY'
        raise JobError(msg) from None


def normalize_range_bound(value: str | None) -> str | None:
    """Turn an `M/D/YYYY` range bound into `YYYY-MM-DD`.

    A blank stays blank and a number passes through. An impossible calendar day
    is rejected. Any other text is a relative date or a date computed at run time.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return value
    if _NUMBER.fullmatch(text):
        return text
    us = _US_DATE.fullmatch(text)
    if us is not None:
        month, day, year = (int(part) for part in us.groups())
        return _calendar_day(year, month, day, value).isoformat()
    iso = _ISO_DATE.fullmatch(text)
    if iso is not None:
        year, month, day = (int(part) for part in iso.groups())
        return _calendar_day(year, month, day, value).isoformat()
    msg = (
        f'{value!r} is a relative date or a date computed at run time; '
        'use YYYY-MM-DD or M/D/YYYY'
    )
    raise JobError(msg)


def _accept_range_bound(value: str | None) -> str | None:
    """Keep the bound the user typed when it is safe to apply later."""
    normalize_range_bound(value)
    return value


def filter_payload(item: ValuesFilter | RangeFilter) -> dict[str, Any]:
    raw = asdict(item)
    if isinstance(item, RangeFilter):
        raw['min'] = normalize_range_bound(item.min)
        raw['max'] = normalize_range_bound(item.max)
    return raw


def normalize_csv(raw: bytes) -> str:
    """Tableau's crosstab "CSV" is UTF-16 and tab-separated; rewrite it as plain UTF-8 CSV."""
    if not raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode('utf-8-sig')
    out = io.StringIO()
    csv.writer(out, lineterminator='\n').writerows(
        csv.reader(io.StringIO(raw.decode('utf-16')), delimiter='\t')
    )
    return out.getvalue()


def _slug(text: str) -> str:
    return re.sub(r'[^\w.-]+', '_', text).strip('_') or 'export'


def output_path(out_dir: Path, job: Job, sheet: str) -> Path:
    return out_dir / _slug(job.name) / f'{_slug(sheet)}.csv'


def _refuse_story(page: Page) -> None:
    """Refuse a story the same way interactive add does, once the view has loaded."""
    try:
        page.evaluate(STORY_JS)
    except PlaywrightError as e:
        if STORY_REFUSAL not in str(e):
            raise
        msg = STORY_REFUSAL
        raise JobError(msg) from e


def open_view(context: BrowserContext, settings: Settings, view: str) -> Page:
    page = context.new_page()
    host = settings.server + HOST_PATH
    html = HOST_PAGE.substitute(
        script_url=json.dumps(
            f'{settings.server}/javascripts/api/tableau.embedding.3.latest.min.js'
        ),
        view_url=json.dumps(settings.view_url(view)),
    )
    page.route(host, lambda route: route.fulfill(content_type='text/html', body=html))
    page.goto(host)
    try:
        page.wait_for_function('() => window.vizState', timeout=VIZ_LOAD_TIMEOUT_MS)
    except PlaywrightTimeoutError as e:
        msg = f'{view!r} did not load within {VIZ_LOAD_TIMEOUT_MS // 1000}s. Check the path and that you can open it in a browser.'
        raise JobError(msg) from e
    state = page.evaluate('() => window.vizState')
    if state != 'ready':
        msg = f'{view!r}: {state}'
        raise JobError(msg)
    try:
        _refuse_story(page)
    except (JobError, PlaywrightError):
        page.close()
        raise
    return page


def export_embed(
    context: BrowserContext,
    settings: Settings,
    job: Job,
    out_dir: Path,
    *,
    on_sheet: Callable[[int, str], None] | None = None,
) -> list[Path]:
    """Crosstab each sheet to CSV; `on_sheet(done, sheet)` fires before each export."""
    filters = resolved_filters(job)
    payloads = [filter_payload(item) for item in filters]
    page = open_view(context, settings, job.view)
    try:
        page.evaluate(
            APPLY_JS,
            {
                'filters': payloads,
                'params': job.params,
            },
        )
        written = []
        for sheet in job.sheets:
            if on_sheet is not None:
                on_sheet(len(written), sheet)
            with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download:
                page.evaluate(EXPORT_JS, sheet)
            path = output_path(out_dir, job, sheet)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                normalize_csv(Path(download.value.path()).read_bytes()),
                encoding='utf-8',
            )
            written.append(path)
        return written
    finally:
        page.close()


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
        # Progress redirects stdout, so site lines, filter notes, and SSO prompts still print.
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
        self.rows.append((job.name, True, ', '.join(map(_home_path, paths))))

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


type _Report = _PlainRun | _TTYRun


def _fail_all(site_jobs: Sequence[Job], message: str, report: _Report) -> list[str]:
    for job in site_jobs:
        report.fail(job, message)
    return [job.name for job in site_jobs]


def _export_group(
    pw: Playwright,
    site_name: str,
    site_jobs: Sequence[Job],
    out_dir: Path,
    report: _Report,
) -> list[str]:
    try:
        settings = load_site(site_name)
    except (UnknownSiteError, MissingSettingsError, ValueError) as e:
        return _fail_all(site_jobs, str(e), report)
    print(
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


def run_jobs(jobs: Sequence[Job], out_dir: Path, report: _Report) -> list[str]:
    """Export every job, carrying on past failures. Returns the failed job names."""
    groups: list[tuple[str, list[Job]]] = []
    for job in jobs:
        if groups and groups[-1][0] == job.site:
            groups[-1][1].append(job)
        else:
            groups.append((job.site, [job]))
    failed: list[str] = []
    if not groups:
        return failed
    with report.live(), sync_playwright() as pw:
        for site_name, site_jobs in groups:
            failed += _export_group(pw, site_name, site_jobs, out_dir, report)
    return failed


def _view_path(item: tsc.ViewItem) -> str:
    """REST content URL `Workbook/sheets/View` as the job's `Workbook/View`."""
    return (item.content_url or '').replace('/sheets/', '/', 1)


def match_score(query: str, text: str) -> float:
    """1.0 when every query word is inside some word of `text`, lower for the closest misspelling."""
    words = _WORD_RE.findall(text.lower())
    terms = _WORD_RE.findall(query.lower())
    if not words or not terms:
        return 0.0
    return min(
        max(1.0 if t in w else SequenceMatcher(None, t, w).ratio() for w in words)
        for t in terms
    )


def _split_values(value: str) -> list[str]:
    return [v.strip() for v in value.split('|')]


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


def _range_ends(current: str) -> tuple[str, str]:
    low, sep, high = current.partition(' .. ')
    if not sep:
        return '', ''
    return low, high


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
    return _accept_range_bound(answer.strip() or None)


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
            low, high = _range_ends(item['current'])
            filters.append(
                RangeFilter(
                    field_name,
                    sheet,
                    _prompt_bound(f'{field_name} from', low),
                    _prompt_bound(f'{field_name} to', high),
                )
            )
        else:
            default = '' if item['current'] == '(All)' else item['current']
            values = _prompt_text(f'{field_name} values (a|b)', default=default)
            filters.append(ValuesFilter(field_name, _split_values(values), sheet))


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


def _prompt_sheets(names: list[str]) -> list[str]:
    return ui.ask(
        questionary.checkbox(
            'Sheets to crosstab',
            choices=names,
            validate=lambda picked: bool(picked) or 'Pick at least one sheet',
            style=ui.STYLE,
        )
    )


def _build_embed_job(
    context: BrowserContext, settings: Settings, name: str, view: str
) -> Job:
    with ui.console.status(f'Opening {view} in headless browser…'):
        page = open_view(context, settings, view)
        info: ViewInfo = page.evaluate(INSPECT_JS)
        page.close()
    chosen = _prompt_sheets([sheet['name'] for sheet in info['sheets']])
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


def _saved_filter(item: ValuesFilter | RangeFilter) -> str:
    if isinstance(item, RangeFilter):
        shown = f'{item.min or ""}..{item.max or ""}'
    else:
        shown = '|'.join(item.values)
    return f'{item.field}={shown} @{item.sheet}'


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
    filters = '; '.join(_saved_filter(item) for item in job.filters) or '-'
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
    default_name = _slug(item.name or view)
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


def parse_filter_spec(spec: str) -> ValuesFilter | RangeFilter:
    """Parse `Field=a|b` or `Field=min..max`, with an optional ` @Sheet`.

    Either side of `min..max` may be empty (`min..` or `..max`), not both. A relative date
    or a date computed at run time is refused.
    """
    body, sep, sheet = spec.rpartition(' @')
    if not sep:
        body, sheet = spec, ''
    field_name, eq, value = body.partition('=')
    field_name = field_name.strip()
    sheet = sheet.strip()
    if not eq or not field_name:
        msg = (
            f'filter {spec!r} should look like Field=a|b or Field=min..max, '
            'with an optional " @Sheet"'
        )
        raise JobError(msg)
    if '..' in value:
        low, _, high = value.partition('..')
        if not low.strip() and not high.strip():
            msg = (
                f'filter {spec!r} should look like Field=a|b or Field=min..max, '
                'with an optional " @Sheet"'
            )
            raise JobError(msg)
        return RangeFilter(
            field_name,
            sheet,
            _accept_range_bound(low.strip() or None),
            _accept_range_bound(high.strip() or None),
        )
    return ValuesFilter(field_name, _split_values(value), sheet)


def parse_param_spec(spec: str) -> tuple[str, str]:
    key, sep, value = spec.partition('=')
    if not sep or not key.strip():
        msg = f'param {spec!r} should look like Name=value'
        raise JobError(msg)
    return key.strip(), value.strip()


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


def _require_flag_shape(args: argparse.Namespace) -> None:
    if not args.view:
        msg = (
            'Pass --view Workbook/View, and --sheet at least once, '
            'to add a job without prompts.'
        )
        raise SystemExit(msg)
    if not args.sheets:
        msg = 'Pass --sheet at least once to add a job without prompts.'
        raise SystemExit(msg)


def add_job_from_flags(
    settings: Settings, args: argparse.Namespace, jobs_file: Path
) -> None:
    view = view_from_flag(args.view)
    filters = [parse_filter_spec(spec) for spec in args.filter_specs or []]
    params = dict(parse_param_spec(spec) for spec in args.params or [])
    name = args.name or _slug(view)
    if any(job.name == name for job in load_jobs(jobs_file)):
        msg = f'A job named {name!r} already exists in {jobs_file}.'
        raise SystemExit(msg)
    _using_site(settings)
    with sync_playwright() as pw:
        page = open_view(browser_session(pw, settings), settings, view)
        page.close()
    job = Job(name, view, list(args.sheets), settings.name, filters, params)
    append_job(jobs_file, replace(job, filters=resolved_filters(job)))


def _flag_mode(args: argparse.Namespace) -> bool:
    return bool(args.view or args.sheets or args.filter_specs or args.params)


def _configured_name(explicit: str | None) -> str:
    if explicit:
        try:
            return check_site_name(explicit)
        except ValueError as e:
            raise SystemExit(str(e)) from e
    names = list_sites()
    if len(names) == 1:
        return names[0]
    if not names:
        msg = 'No Tableau site configured. Run: tabpull setup'
    else:
        msg = f'Pass --site. Configured sites: {", ".join(names)}'
    raise SystemExit(msg)


def _pick_site(explicit: str | None) -> Settings:
    if explicit or not ui.interactive():
        return load_site(_configured_name(explicit))
    names = list_sites()
    if len(names) == 1:
        return load_site(names[0])
    if not names:
        print('No Tableau site configured. Starting setup.')
        return load_site(wizard.main(None))
    choices = []
    for name in names:
        settings = load_site(name)
        label = f'{name}  ({settings.server}, site {settings.site or "(default)"})'
        choices.append(questionary.Choice(label, value=name))
    return load_site(
        ui.ask(questionary.select('Site', choices=choices, style=ui.STYLE))
    )


def _cmd_login(site: str | None) -> None:
    try:
        settings = _pick_site(site)
    except (UnknownSiteError, MissingSettingsError, ValueError) as e:
        raise SystemExit(str(e)) from e
    print(
        f'Signing in to {settings.server}, site {settings.site or "(default)"} '
        f'(local name {settings.name}).'
    )
    with sync_playwright() as pw:
        sso_login(pw, settings)


def _cmd_add(args: argparse.Namespace, jobs_file: Path) -> None:
    try:
        if _flag_mode(args) or not ui.interactive():
            _require_flag_shape(args)
            add_job_from_flags(load_site(_configured_name(args.site)), args, jobs_file)
            return
        ui.console.print(Rule('[bold]tabpull add', align='left', style='blue'))
        add_job(_pick_site(args.site), jobs_file, args.name)
    except (
        JobError,
        UnknownSiteError,
        MissingSettingsError,
        PlaywrightError,
        tomllib.TOMLDecodeError,
        OSError,
        ValueError,
    ) as e:
        message = str(e).partition('\n')[0] or repr(e)
        raise SystemExit(message) from e


def _file_flags(args: argparse.Namespace) -> list[str]:
    """The --jobs/--out flags this invocation used, to carry into suggested commands."""
    flags = []
    if args.jobs:
        flags += ['--jobs', str(args.jobs)]
    if args.out:
        flags += ['--out', str(args.out)]
    return flags


def _run_flags(args: argparse.Namespace) -> list[str]:
    """File flags plus this run's filter overrides, for the suggested rerun."""
    flags = _file_flags(args)
    for spec in args.filter_specs or []:
        flags += ['--filter', spec]
    return flags


def _cmd_run(args: argparse.Namespace, jobs_file: Path, out_dir: Path) -> int:
    try:
        jobs = load_jobs(jobs_file)
    except (JobError, tomllib.TOMLDecodeError, OSError) as e:
        msg = f'{jobs_file}: {e}'
        raise SystemExit(msg) from e
    if not jobs:
        msg = f'No jobs in {jobs_file}. Run: tabpull add'
        raise SystemExit(msg)
    if unknown := set(args.names) - {job.name for job in jobs}:
        msg = (
            f'Unknown jobs: {", ".join(sorted(unknown))}. '
            f'Saved jobs: {", ".join(job.name for job in jobs)}'
        )
        raise SystemExit(msg)
    selected = [job for job in jobs if not args.names or job.name in args.names]
    try:
        overrides = [parse_filter_spec(spec) for spec in args.filter_specs or []]
        selected = [filters_for_run(job, overrides) for job in selected]
    except JobError as e:
        raise SystemExit(str(e)) from e
    report = (_TTYRun if ui.rich_output() else _PlainRun)(selected, out_dir)
    failed = run_jobs(selected, out_dir, report)
    # Flags before `--` so a job named like an option (`-daily`, `-h`) stays a name.
    rerun = shlex.join(['tabpull', 'run', *_run_flags(args), '--', *failed])
    report.summary(
        len(selected) - len(failed), len(selected), rerun if failed else None
    )
    return 1 if failed else 0


_TOON_NUMBER = re.compile(r'^[+-]?[0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?$', re.IGNORECASE)
_TOON_QUOTE = re.compile(r'[,:"\\\[\]{}\x00-\x1f]')


def _toon(value: object) -> str:
    """One TOON value, quoted only when the spec requires it (comma delimiter)."""
    text = str(value)
    if isinstance(value, int):
        return text
    if (
        text in {'', 'true', 'false', 'null'}
        or text != text.strip(' \t')
        or text.startswith(('-', '#'))
        or _TOON_NUMBER.match(text)
        or _TOON_QUOTE.search(text)
    ):
        return json.dumps(text, ensure_ascii=False)
    return text


def _toon_table(
    name: str, fields: Sequence[str], rows: Sequence[Sequence[object]]
) -> list[str]:
    head = f'{name}[{len(rows)}]{{{",".join(fields)}}}:'
    return [head, *('  ' + ','.join(map(_toon, row)) for row in rows)]


def _home_path(path: Path | str) -> str:
    text = os.path.normpath(Path(path).absolute())
    home = str(Path.home())
    return '~' + text[len(home) :] if text.startswith(home + os.sep) else text


def _sso_badge(name: str, server: str) -> str:
    if server == '(incomplete)':
        return '[red]incomplete[/]'
    if load_site(name).auth_path.exists():
        return '[green]● saved[/]'
    return '[yellow]● not signed in[/]'


def _home_help(
    args: argparse.Namespace,
    sites: Sequence[Sequence[str]],
    jobs: Sequence[Job],
) -> list[str]:
    help_lines: list[str] = []
    if not sites:
        help_lines.append('Run `tabpull setup` to connect a Tableau site')
    quoted = shlex.join(_file_flags(args))
    flags = f' {quoted}' if quoted else ''
    if jobs:
        help_lines += [
            f'Run `tabpull run{flags}` to export every job into the out folder',
            f'Run `tabpull run <name>{flags}` to export one job',
        ]
    if sites:
        jobs_flag = f' --jobs {shlex.quote(str(args.jobs))}' if args.jobs else ''
        help_lines.append(
            f'Run `tabpull add --view <Workbook/View> --sheet "<sheet>"{jobs_flag}` to save a job'
        )
    return help_lines


def _print_home(
    sites: Sequence[Sequence[str]],
    jobs: Sequence[Job],
    jobs_error: str | None,
    jobs_file: Path,
    out_dir: Path,
) -> None:
    if sites:
        sites_table = Table(box=box.SIMPLE_HEAD, expand=True)
        for col in ('site', 'server', 'tableau site', 'sso'):
            sites_table.add_column(col)
        for name, server, site in sites:
            sites_table.add_row(
                f'[bold]{escape(name)}[/]',
                escape(server),
                escape(site),
                _sso_badge(name, server),
            )
        sites_body: Table | str = sites_table
    else:
        sites_body = '0 configured'
    ui.console.print(
        Panel(
            sites_body,
            title='[bold]Sites[/]',
            title_align='left',
            border_style='blue',
        )
    )
    jobs_body: Table | str
    if jobs_error is not None:
        jobs_body = escape(jobs_error)
    elif jobs:
        jobs_table = Table(box=box.SIMPLE_HEAD, expand=True)
        for col in ('job', 'site', 'view', 'sheets', 'filters'):
            jobs_table.add_column(col)
        for job in jobs:
            jobs_table.add_row(
                f'[bold]{escape(job.name)}[/]',
                escape(job.site),
                escape(job.view),
                '\n'.join(escape(sheet) for sheet in job.sheets),
                str(len(job.filters)),
            )
        jobs_body = jobs_table
    else:
        jobs_body = '0 saved'
    ui.console.print(
        Panel(jobs_body, title='[bold]Jobs[/]', title_align='left', border_style='blue')
    )
    ui.console.print(
        f'  [dim]jobs file[/] {escape(_home_path(jobs_file))}\n'
        f'  [dim]exports  [/] {escape(_home_path(out_dir))}/<job>/<sheet>.csv'
    )


def _site_rows() -> list[list[str]]:
    rows = []
    for name in list_sites():
        try:
            settings = load_site(name)
        except (MissingSettingsError, ValueError):
            rows.append([name, '(incomplete)', ''])
        else:
            rows.append([name, settings.server, settings.site])
    return rows


def _cmd_home(args: argparse.Namespace, jobs_file: Path, out_dir: Path) -> None:
    """What an agent or a person needs first: sites, jobs, and where files go."""
    sites = _site_rows()
    jobs_error: str | None = None
    try:
        jobs = load_jobs(jobs_file)
    except (JobError, tomllib.TOMLDecodeError, OSError) as e:
        jobs_error = f'unreadable: {e}'
        jobs = []
    help_lines = _home_help(args, sites, jobs)
    if ui.rich_output():
        _print_home(sites, jobs, jobs_error, jobs_file, out_dir)
        if help_lines:
            body = '\n'.join(f'  {escape(line)}' for line in help_lines)
            ui.console.print(f'\n  [bold]next[/]\n{body}\n')
        return
    lines = [
        f'bin: {_toon(_home_path(sys.argv[0]))}',
        'description: Export Tableau dashboard sheet crosstabs to CSV',
        f'jobs_file: {_toon(_home_path(jobs_file))}',
        f'out: {_toon(_home_path(out_dir))}',
    ]
    if sites:
        lines += _toon_table('sites', ('name', 'server', 'site'), sites)
    else:
        lines.append('sites: 0 configured')
    if jobs_error is not None:
        lines.append(f'jobs: {_toon(jobs_error)}')
    elif jobs:
        rows = [[j.name, j.site, j.view, len(j.sheets)] for j in jobs]
        lines += _toon_table('jobs', ('name', 'site', 'view', 'sheets'), rows)
    else:
        lines.append('jobs: 0 saved')
    lines.append(f'help[{len(help_lines)}]:')
    lines += [f'  {line}' for line in help_lines]
    print('\n'.join(lines))


class _Parser(argparse.ArgumentParser):
    """Usage errors on stdout with this command's usage, so the fix is one step."""

    def error(self, message: str) -> NoReturn:
        print(f'error: {message}')
        print(self.format_usage().rstrip())
        print(f'help: run `{self.prog} --help` for flags and examples')
        sys.exit(2)


def _add_file_flags(
    parser: argparse.ArgumentParser, *, out: bool, default: object
) -> None:
    parser.add_argument(
        '--jobs',
        type=Path,
        default=default,
        help='jobs file for this run (default: the config directory jobs.toml)',
    )
    if out:
        parser.add_argument(
            '--out',
            type=Path,
            default=default,
            help='output folder for this run (default: the current folder)',
        )


def _parser() -> tuple[_Parser, dict[str, _Parser]]:
    parser = _Parser(
        prog='tabpull',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(*VERSION_FLAGS, action='version', version=version())
    _add_file_flags(parser, out=True, default=None)
    commands = parser.add_subparsers(dest='command')
    setup = commands.add_parser('setup', help='save a site token and browser session')
    setup.add_argument('--site', help='local name for this site; each job refers to it')
    add = commands.add_parser(
        'add',
        help='save a job, prompting unless view and sheets are flags',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  tabpull add
  tabpull add --view SalesWorkbook/Overview --sheet "Order Detail" --name daily
  tabpull add --view SalesWorkbook/Overview --sheet Totals \\
    --filter "Region=West|Central" --filter "Order Date=2026-09-01..2026-09-25"
""",
    )
    add.add_argument('--site', help='local site name from tabpull setup')
    add.add_argument('--view', help='Workbook/View, or a view URL')
    add.add_argument('--name', help='job name')
    add.add_argument(
        '--sheet',
        action='append',
        dest='sheets',
        metavar='SHEET',
        help='worksheet to crosstab (repeat for several)',
    )
    add.add_argument(
        '--filter',
        action='append',
        dest='filter_specs',
        metavar='SPEC',
        help='Field=a|b or Field=min..max (min.. or ..max leaves that side open); optional " @Sheet" (repeatable)',
    )
    add.add_argument(
        '--param',
        action='append',
        dest='params',
        metavar='NAME=VALUE',
        help='parameter Name=value (repeatable)',
    )
    _add_file_flags(add, out=False, default=argparse.SUPPRESS)
    run = commands.add_parser(
        'run',
        help='export jobs into the current folder',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""writes <out>/<job>/<sheet>.csv, spaces in names become _

--filter uses the same Field=a|b or Field=min..max syntax as add, including
an open side (min.. or ..max). It applies to every job in this run and
does not rewrite the jobs file. With no @Sheet it replaces the job's saved
filter on that field on every sheet; with @Sheet, only that sheet's. A field
the job does not filter is added, on @Sheet or else the first sheet.

examples:
  tabpull run
  tabpull run daily-west
  tabpull run daily-west --filter "Order Date=2026-09-01.." --out ~/reports
  tabpull run daily-west weekly-east --filter "Order Date=2026-09-01..2026-09-07"
""",
    )
    run.add_argument('names', nargs='*', help='only these jobs (default: all)')
    run.add_argument(
        '--filter',
        action='append',
        dest='filter_specs',
        metavar='SPEC',
        help=(
            'override Field=a|b or Field=min..max for this run only '
            '(repeatable; applied to every selected job; jobs file unchanged)'
        ),
    )
    _add_file_flags(run, out=True, default=argparse.SUPPRESS)
    login = commands.add_parser('login', help='refresh a site SSO browser session')
    login.add_argument(
        '--site', help='local site name (default: the only configured site)'
    )
    return parser, commands.choices


def main(argv: Sequence[str] | None = None) -> int:
    parser, commands = _parser()
    args, extra = parser.parse_known_args(argv)
    if extra:
        (commands.get(args.command) or parser).error(
            f'unrecognized arguments: {" ".join(extra)}'
        )
    jobs_file = args.jobs or jobs_path()
    out_dir = args.out or Path()
    match args.command:
        case 'setup':
            wizard.main(args.site)
        case 'login':
            _cmd_login(args.site)
        case 'add':
            _cmd_add(args, jobs_file)
        case 'run':
            return _cmd_run(args, jobs_file, out_dir)
        case _:
            _cmd_home(args, jobs_file, out_dir)
    return 0


def cli() -> None:
    """Print a failure as `error: ...` on stdout, where agents read the rest."""
    try:
        code = main()
    except KeyboardInterrupt:
        print(file=sys.stderr)
        code = 130
    except SystemExit as e:
        if not isinstance(e.code, str):
            raise
        print(f'error: {e.code}')
        code = 1
    sys.exit(code)


if __name__ == '__main__':
    print('Run: tabpull setup | add | run | login', file=sys.stderr)
    sys.exit(2)
