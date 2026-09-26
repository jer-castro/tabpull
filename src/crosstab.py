"""Export Tableau crosstabs from specific dashboard sheets, with filters, as CSV.

Commands:
  add    find a view with your PAT, pick sheets and filters, save it as a job
  run    export every job (or the named ones) in the jobs file
  login  refresh the SSO browser session
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
from dataclasses import asdict, dataclass, field
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, TypedDict

import tableauserverclient as tsc
from playwright.sync_api import BrowserContext, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import wizard
from tableau import (
    MissingSettingsError,
    Settings,
    browser_session,
    load_settings,
    parse_tableau_url,
    rest_session,
    sso_login,
)

DEFAULT_JOBS = Path('jobs.toml')
DEFAULT_OUT = Path('exports')
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


def _parse_filter(
    raw: dict[str, Any], default_sheet: str
) -> ValuesFilter | RangeFilter:
    raw = {'sheet': default_sheet} | raw
    return ValuesFilter(**raw) if 'values' in raw else RangeFilter(**raw)


def parse_job(raw: object) -> Job:
    if not isinstance(raw, dict):
        msg = f'job {raw!r} is not a table'
        raise JobError(msg)
    raw = dict(raw)
    if not raw.get('sheets'):
        msg = f'job {raw.get("name", "?")!r}: needs at least one sheet'
        raise JobError(msg)
    try:
        filters = [_parse_filter(f, raw['sheets'][0]) for f in raw.pop('filters', [])]
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
        {k: v for k, v in f.items() if v or isinstance(v, list)}
        for f in fields['filters']
    ]
    lines = [
        '[[job]]',
        f'name = {_toml(fields.pop("name"))}',
    ]
    lines += [f'{key} = {_toml(value)}' for key, value in fields.items() if value]
    return '\n'.join(lines) + '\n'


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
    return page


def export_embed(
    context: BrowserContext, settings: Settings, job: Job, out_dir: Path
) -> list[Path]:
    page = open_view(context, settings, job.view)
    try:
        page.evaluate(
            APPLY_JS,
            {
                'filters': [filter_payload(f) for f in job.filters],
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


def run_jobs(settings: Settings, jobs: Sequence[Job], out_dir: Path) -> int:
    """Export every job, carrying on past failures. Returns the number of failed jobs."""
    failed = 0
    if not jobs:
        return 0
    with sync_playwright() as pw:
        context = browser_session(pw, settings)
        for job in jobs:
            try:
                paths = export_embed(context, settings, job, out_dir)
            except (
                JobError,
                PlaywrightError,
                OSError,
                UnicodeError,
                csv.Error,
            ) as e:
                failed += 1
                print(
                    f'  ✗ {job.name}: {str(e).partition("\n")[0] or repr(e)}'
                )  # Playwright appends the JS stack
            else:
                print(f'  ✓ {job.name}: {", ".join(map(str, paths))}')
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
        for f in sheet['filters']:
            by_field.setdefault(f['field'], (sheet['name'], f['type']))
            print(
                f'  filter  {f["field"]} ({f["type"]}) on {sheet["name"]!r}: {f["current"]}'
            )
    for p in info['params']:
        print(f'  param   {p["name"]}: {p["current"]}')

    filters: list[ValuesFilter | RangeFilter] = []
    for key, value in _prompt_pairs(
        'Filters as Field=value, | between values, min..max for ranges. Blank line when done.'
    ):
        sheet, kind = by_field.get(key, (chosen[0], 'categorical'))
        if key not in by_field:
            print(
                f'  {key!r} is not a filter on these sheets; applying it to {sheet!r} anyway.'
            )
        if kind == 'range':
            low, _, high = value.partition('..')
            filters.append(
                RangeFilter(key, sheet, low.strip() or None, high.strip() or None)
            )
        else:
            filters.append(ValuesFilter(key, _split_values(value), sheet))
    params = dict(_prompt_pairs('Parameters as Name=value. Blank line when done.'))
    return Job(name, view, chosen, filters, params)


def add_job(settings: Settings, jobs_path: Path) -> None:
    item = _find_view(settings)
    view = _view_path(item)
    existing = {j.name for j in load_jobs(jobs_path)}
    default_name = _slug(item.name or view)
    name = input(f'Job name [{default_name}]: ').strip() or default_name
    if name in existing:
        msg = f'A job named {name!r} already exists in {jobs_path}.'
        raise SystemExit(msg)

    with sync_playwright() as pw:
        job = _build_embed_job(browser_session(pw, settings), settings, name, view)
    text = job_to_toml(job)
    with jobs_path.open('a', encoding='utf-8') as f:
        f.write('\n' + text)
    print(f'\nSaved job {name!r} to {jobs_path}:\n\n{text}')
    print(f'Run it: uv run src/crosstab.py run {name}')


def _settings() -> Settings:
    try:
        return load_settings()
    except MissingSettingsError as e:
        if not sys.stdin.isatty():
            msg = f'{e}. Run: uv run src/wizard.py'
            raise SystemExit(msg) from e
        print(f'{e}. Starting setup.')
        wizard.main()
        return load_settings()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        '--jobs',
        type=Path,
        default=DEFAULT_JOBS,
        help='jobs file (default: %(default)s)',
    )
    parser.add_argument(
        '--out',
        type=Path,
        default=DEFAULT_OUT,
        help='output folder (default: %(default)s)',
    )
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('add', help='find a view, pick sheets and filters, save a job')
    run = commands.add_parser('run', help='export jobs')
    run.add_argument('names', nargs='*', help='only these jobs (default: all)')
    commands.add_parser('login', help='refresh the SSO browser session')
    args = parser.parse_args(argv)

    settings = _settings()
    match args.command:
        case 'login':
            with sync_playwright() as pw:
                sso_login(pw, settings)
        case 'add':
            try:
                add_job(settings, args.jobs)
            except (
                JobError,
                PlaywrightError,
                tomllib.TOMLDecodeError,
                OSError,
            ) as e:
                raise SystemExit(str(e).partition('\n')[0] or repr(e)) from e
            return 0
        case 'run':
            try:
                jobs = load_jobs(args.jobs)
            except (JobError, tomllib.TOMLDecodeError) as e:
                msg = f'{args.jobs}: {e}'
                raise SystemExit(msg) from e
            unknown = set(args.names) - {j.name for j in jobs}
            if unknown or not jobs:
                msg = (
                    f'Unknown jobs: {", ".join(sorted(unknown))}'
                    if unknown
                    else f'No jobs in {args.jobs}. Run: uv run src/crosstab.py add'
                )
                raise SystemExit(msg)
            selected = [j for j in jobs if not args.names or j.name in args.names]
            print(f'Exporting {len(selected)} job(s) to {args.out}/')
            return 1 if run_jobs(settings, selected, args.out) else 0
    return 0


def cli() -> None:
    sys.exit(main())


if __name__ == '__main__':
    cli()
