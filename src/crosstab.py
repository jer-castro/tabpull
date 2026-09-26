"""Export Tableau crosstab CSVs for the dashboard sheets you name.

Commands:
  setup  save a site's personal access token and SSO session
  add    record a job (prompts, or flags for the site, view, sheets, and filters)
  run    export jobs from the jobs file
  login  refresh a site's SSO session
"""

import argparse
import codecs
import csv
import io
import json
import re
import string
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, TypedDict

import tableauserverclient as tsc
from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import wizard
from tableau import (
    MissingSettingsError,
    Settings,
    UnknownSiteError,
    browser_session,
    check_site_name,
    exports_dir,
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
_US_DATE = re.compile(r'^(\d{1,2})/(\d{1,2})/(\d{4})$')
# A fake page on the Tableau origin: the embedded viz then loads first-party, so the SSO
# cookies apply and "restrict embedding to these domains" site settings can't block it.
HOST_PATH = '/__crosstab_host__'

HOST_PAGE = string.Template("""<!doctype html>
<meta charset="utf-8">
<body>
<script type="module">
try {
  const api = await import($script_url);
  window.tableauEnums = { CrosstabFileFormat: api.CrosstabFileFormat, FilterUpdateType: api.FilterUpdateType };
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

STORY_JS = """() => {
  if (window.viz.workbook.activeSheet.sheetType === 'story') throw new Error('Stories are not supported; use the dashboard inside it.');
}"""

INSPECT_JS = """async () => {
  const active = window.viz.workbook.activeSheet;
  if (active.sheetType === 'story') throw new Error('Stories are not supported; use the dashboard inside it.');
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

APPLY_JS = """async ({ filters, params }) => {
  const viz = window.viz;
  const active = viz.workbook.activeSheet;
  const worksheets = active.sheetType === 'worksheet' ? [active] : active.worksheets;
  const toValue = (v) => {
    if (typeof v !== 'string') return v;
    // Tableau reads range-filter Dates as UTC calendar days, whatever the browser timezone.
    if (/^\\d{4}-\\d{2}-\\d{2}$/.test(v)) return new Date(v + 'T00:00:00Z');
    if (/^-?\\d+(\\.\\d+)?$/.test(v)) return Number(v);
    return v;
  };
  for (const [name, value] of Object.entries(params)) await viz.workbook.changeParameterValueAsync(name, value);
  for (const f of filters) {
    const ws = worksheets.find((w) => w.name === f.sheet);
    if (!ws) throw new Error('Sheet not in this view: ' + f.sheet);
    if (f.values) await ws.applyFilterAsync(f.field, f.values, window.tableauEnums.FilterUpdateType.Replace);
    else await ws.applyRangeFilterAsync(f.field, { min: toValue(f.min), max: toValue(f.max) });
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
        print(
            f'  {job.name}: filter {item.field!r} names no sheet; '
            f'applying it on {default!r}, the first sheet in the job.'
        )
        resolved.append(replace(item, sheet=default))
    return resolved


def normalize_range_bound(value: str | None) -> str | None:
    """Turn an `M/D/YYYY` range bound into `YYYY-MM-DD`. Other values pass through."""
    if value is None:
        return None
    match = _US_DATE.fullmatch(value.strip())
    if match is None:
        return value
    month, day, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        msg = f'{value!r} is not a real date; use YYYY-MM-DD or M/D/YYYY'
        raise JobError(msg) from None


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
        if 'Stories are not supported' not in str(e):
            raise
        msg = 'Stories are not supported; use the dashboard inside it.'
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
    context: BrowserContext, settings: Settings, job: Job, out_dir: Path
) -> list[Path]:
    filters = resolved_filters(job)
    page = open_view(context, settings, job.view)
    try:
        page.evaluate(
            APPLY_JS,
            {
                'filters': [filter_payload(item) for item in filters],
                'params': job.params,
            },
        )
        written = []
        for sheet in job.sheets:
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


def _fail(name: str, message: str) -> None:
    print(f'  ✗ {name}: {message.partition("\n")[0] or message}')


def _export_group(
    pw: Playwright, site_name: str, site_jobs: Sequence[Job], out_dir: Path
) -> int:
    try:
        settings = load_site(site_name)
    except (UnknownSiteError, MissingSettingsError, ValueError) as e:
        for job in site_jobs:
            _fail(job.name, str(e))
        return len(site_jobs)
    print(
        f'  site {settings.name}: {settings.server}, site {settings.site or "(default)"}'
    )
    try:
        context = browser_session(pw, settings)
    except SystemExit as e:
        message = (
            e.code if isinstance(e.code, str) else 'could not open a browser session'
        )
        for job in site_jobs:
            _fail(job.name, message)
        return len(site_jobs)
    failed = 0
    for job in site_jobs:
        try:
            paths = export_embed(context, settings, job, out_dir)
        except (JobError, PlaywrightError, OSError, UnicodeError, csv.Error) as e:
            failed += 1
            _fail(job.name, str(e))
        else:
            print(f'  ✓ {job.name}: {", ".join(map(str, paths))}')
    return failed


def run_jobs(jobs: Sequence[Job], out_dir: Path) -> int:
    """Export every job, carrying on past failures. Returns the number of failed jobs."""
    groups: list[tuple[str, list[Job]]] = []
    for job in jobs:
        if groups and groups[-1][0] == job.site:
            groups[-1][1].append(job)
        else:
            groups.append((job.site, [job]))
    if not groups:
        return 0
    failed = 0
    with sync_playwright() as pw:
        for site_name, site_jobs in groups:
            failed += _export_group(pw, site_name, site_jobs, out_dir)
    return failed


def _choose(prompt: str, count: int) -> list[int]:
    while True:
        answer = input(f'{prompt} ').strip()
        try:
            picks = [int(part) - 1 for part in answer.replace(',', ' ').split()]
        except ValueError:
            picks = []
        if picks and all(0 <= p < count for p in picks):
            return picks
        print(f'  Enter numbers between 1 and {count}.')


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


def _find_view(settings: Settings) -> tsc.ViewItem:
    query = input(
        'Paste a Tableau view URL, or type part of a workbook/view name: '
    ).strip()
    with rest_session(settings) as server:
        options = tsc.RequestOptions(pagesize=1000)
        options.fields |= {'_default_', 'sheetType'}
        views = list(tsc.Pager(server.views, options))
    target = parse_tableau_url(query).view if query.startswith('http') else None
    if target:
        matches = [v for v in views if _view_path(v) == target]
    else:
        scored = [(match_score(query, f'{v.name} {v.content_url}'), v) for v in views]
        scored.sort(key=lambda sv: -sv[0])
        matches = [v for score, v in scored if score >= MIN_MATCH_SCORE]
    if not matches:
        msg = f'No views you can access match {query!r}.'
        raise SystemExit(msg)
    if len(matches) > MAX_SEARCH_RESULTS:
        print(
            f'  Showing the best {MAX_SEARCH_RESULTS} of {len(matches)} matches; type more of the name to narrow it.'
        )
        matches = matches[:MAX_SEARCH_RESULTS]
    for i, v in enumerate(matches, 1):
        print(f'  [{i}] {v.name}  ({v.sheet_type or "view"}, {v.content_url})')
    return matches[_choose('Which view?', len(matches))[0]]


def _prompt_pairs(prompt: str) -> list[tuple[str, str]]:
    print(prompt)
    pairs = []
    while line := input('  > ').strip():
        key, sep, value = line.partition('=')
        if sep and key.strip():
            pairs.append((key.strip(), value.strip()))
        else:
            print('  Use Name=value.')
    return pairs


def _build_embed_job(
    context: BrowserContext, settings: Settings, name: str, view: str
) -> Job:
    page = open_view(context, settings, view)
    info: ViewInfo = page.evaluate(INSPECT_JS)
    page.close()
    sheets = info['sheets']
    print('\nSheets in this view (hidden ones included):')
    for i, sheet in enumerate(sheets, 1):
        print(f'  [{i}] {sheet["name"]}')
    chosen = [
        sheets[i]['name']
        for i in _choose('Which sheet(s) to crosstab? (e.g. 2 or 2,3)', len(sheets))
    ]

    by_field: dict[str, tuple[str, str]] = {}
    for sheet in sorted(sheets, key=lambda s: s['name'] not in chosen):
        for item in sheet['filters']:
            by_field.setdefault(item['field'], (sheet['name'], item['type']))
            print(
                f'  filter  {item["field"]} ({item["type"]}) on {sheet["name"]!r}: {item["current"]}'
            )
    for param in info['params']:
        print(f'  param   {param["name"]}: {param["current"]}')

    filters: list[ValuesFilter | RangeFilter] = []
    for key, value in _prompt_pairs(
        'Filters as Field=value, | between values, min..max for ranges. Blank line when done.'
    ):
        sheet, kind = by_field.get(key, (chosen[0], 'categorical'))
        if key not in by_field:
            print(
                f'  {key!r} names no sheet; applying it on {sheet!r}, the first sheet in the job.'
            )
        if kind == 'range':
            low, _, high = value.partition('..')
            filters.append(
                RangeFilter(key, sheet, low.strip() or None, high.strip() or None)
            )
        else:
            filters.append(ValuesFilter(key, _split_values(value), sheet))
    params = dict(_prompt_pairs('Parameters as Name=value. Blank line when done.'))
    return Job(name, view, chosen, settings.name, filters, params)


def _using_site(settings: Settings) -> None:
    print(
        f'Using site {settings.name!r} ({settings.server}, '
        f'site {settings.site or "(default)"}).'
    )


def append_job(path: Path, job: Job) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = job_to_toml(job)
    prefix = '\n' if path.exists() and path.stat().st_size else ''
    with path.open('a', encoding='utf-8') as handle:
        handle.write(prefix + text)
    print(f'\nSaved job {job.name!r} to {path}:\n\n{text}')
    print(f'Run it: tabpull run {job.name}')


def add_job(settings: Settings, jobs_path: Path, name: str | None = None) -> None:
    _using_site(settings)
    item = _find_view(settings)
    view = _view_path(item)
    existing = {job.name for job in load_jobs(jobs_path)}
    default_name = _slug(item.name or view)
    if name is None:
        name = input(f'Job name [{default_name}]: ').strip() or default_name
    if name in existing:
        msg = f'A job named {name!r} already exists in {jobs_path}.'
        raise SystemExit(msg)

    with sync_playwright() as pw:
        job = _build_embed_job(browser_session(pw, settings), settings, name, view)
    append_job(jobs_path, job)


def parse_filter_spec(spec: str) -> ValuesFilter | RangeFilter:
    """Parse `Field=a|b` or `Field=min..max`, with an optional ` @Sheet`."""
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
        return RangeFilter(field_name, sheet, low.strip() or None, high.strip() or None)
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
    if explicit or not sys.stdin.isatty():
        return load_site(_configured_name(explicit))
    names = list_sites()
    if len(names) == 1:
        return load_site(names[0])
    if not names:
        print('No Tableau site configured. Starting setup.')
        return load_site(wizard.main(None))
    print('Sites:')
    for index, name in enumerate(names, 1):
        settings = load_site(name)
        print(
            f'  [{index}] {name}  ({settings.server}, site {settings.site or "(default)"})'
        )
    return load_site(names[_choose('Which site?', len(names))[0]])


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
        if _flag_mode(args):
            _require_flag_shape(args)
            add_job_from_flags(load_site(_configured_name(args.site)), args, jobs_file)
            return
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


def _cmd_run(args: argparse.Namespace, jobs_file: Path, out_dir: Path) -> int:
    try:
        jobs = load_jobs(jobs_file)
    except (JobError, tomllib.TOMLDecodeError, OSError) as e:
        msg = f'{jobs_file}: {e}'
        raise SystemExit(msg) from e
    unknown = set(args.names) - {job.name for job in jobs}
    if unknown or not jobs:
        msg = (
            f'Unknown jobs: {", ".join(sorted(unknown))}'
            if unknown
            else f'No jobs in {jobs_file}. Run: tabpull add'
        )
        raise SystemExit(msg)
    selected = [job for job in jobs if not args.names or job.name in args.names]
    print(f'Exporting {len(selected)} job(s) to {out_dir}/')
    return 1 if run_jobs(selected, out_dir) else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        '--jobs',
        type=Path,
        help='jobs file for this run (default: the config directory jobs.toml)',
    )
    parser.add_argument(
        '--out',
        type=Path,
        help='output folder for this run (default: the data directory exports folder)',
    )
    commands = parser.add_subparsers(dest='command', required=True)
    setup = commands.add_parser('setup', help='save a site token and browser session')
    setup.add_argument('--site', help='local name for this site; each job refers to it')
    add = commands.add_parser(
        'add', help='save a job, prompting unless view and sheets are flags'
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
        help='Field=a|b or Field=min..max, optional " @Sheet" (repeatable)',
    )
    add.add_argument(
        '--param',
        action='append',
        dest='params',
        metavar='NAME=VALUE',
        help='parameter Name=value (repeatable)',
    )
    run = commands.add_parser('run', help='export jobs')
    run.add_argument('names', nargs='*', help='only these jobs (default: all)')
    login = commands.add_parser('login', help='refresh a site SSO browser session')
    login.add_argument(
        '--site', help='local site name (default: the only configured site)'
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    jobs_file = args.jobs or jobs_path()
    out_dir = args.out or exports_dir()
    match args.command:
        case 'setup':
            wizard.main(args.site)
            return 0
        case 'login':
            _cmd_login(args.site)
            return 0
        case 'add':
            _cmd_add(args, jobs_file)
            return 0
        case 'run':
            return _cmd_run(args, jobs_file, out_dir)
    return 0


def cli() -> None:
    sys.exit(main())


if __name__ == '__main__':
    print('Run: tabpull setup | add | run | login', file=sys.stderr)
    sys.exit(2)
