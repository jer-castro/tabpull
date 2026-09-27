import codecs
import csv
import io
import json
import string
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, TypedDict

from playwright.sync_api import BrowserContext, Page
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tabpull.filters import normalize_range_bound, resolved_filters
from tabpull.jobs import Job, JobError, RangeFilter, ValuesFilter, slug
from tabpull.tableau import Settings

VIZ_LOAD_TIMEOUT_MS = 180_000
DOWNLOAD_TIMEOUT_MS = 300_000
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


class SheetInfo(TypedDict):
    name: str
    filters: list[dict[str, str]]


class ViewInfo(TypedDict):
    sheets: list[SheetInfo]
    params: list[dict[str, str]]


def filter_payload(item: ValuesFilter | RangeFilter) -> dict[str, Any]:
    raw = asdict(item)
    if isinstance(item, RangeFilter):
        raw['min'] = normalize_range_bound(item.min)
        raw['max'] = normalize_range_bound(item.max)
    return raw


def normalize_csv(raw: bytes) -> str:
    """Tableau's crosstab "CSV" is UTF-16 and tab-separated."""
    if not raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode('utf-8-sig')
    out = io.StringIO()
    csv.writer(out, lineterminator='\n').writerows(
        csv.reader(io.StringIO(raw.decode('utf-16')), delimiter='\t')
    )
    return out.getvalue()


def output_path(out_dir: Path, job: Job, sheet: str) -> Path:
    return out_dir / slug(job.name) / f'{slug(sheet)}.csv'


def _refuse_story(page: Page) -> None:
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
