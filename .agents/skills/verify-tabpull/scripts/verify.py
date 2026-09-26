#!/usr/bin/env -S uv run python
"""Verification helpers for tabpull: doctor, inspect, date-filter.

Run from the repo root: uv run python .agents/skills/verify-tabpull/scripts/verify.py --help
"""

import argparse
import hashlib
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'src'))

from playwright.sync_api import Browser, BrowserContext, sync_playwright

from crosstab import (
    APPLY_JS,
    INSPECT_JS,
    EmbedJob,
    JobError,
    RangeFilter,
    export_embed,
    filter_payload,
    load_jobs,
    open_view,
)
from tableau import (
    AUTH_STATE,
    MissingSettingsError,
    Settings,
    launch_browser,
    load_settings,
    session_valid,
)

# UTC, west of UTC, east of UTC: a local-date bug shifts the day in at least one of them.
TIMEZONES = ('UTC', 'America/Los_Angeles', 'Pacific/Auckland')

READ_RANGE_JS = """async ({ sheet, field }) => {
  const active = window.viz.workbook.activeSheet;
  const worksheets = active.sheetType === 'worksheet' ? [active] : active.worksheets;
  const ws = worksheets.find((w) => w.name === sheet);
  const f = (await ws.getFiltersAsync()).find((x) => x.fieldName === field);
  if (!f) throw new Error('No filter on ' + field + ' after applying it');
  const out = (v) => v && { value: v.value instanceof Date ? v.value.toISOString() : v.value, formatted: v.formattedValue };
  return { type: f.filterType, min: out(f.minValue), max: out(f.maxValue),
           browser_tz: Intl.DateTimeFormat().resolvedOptions().timeZone };
}"""


def _browser_context(
    browser: Browser, settings: Settings, timezone: str = 'UTC'
) -> BrowserContext:
    context = browser.new_context(storage_state=AUTH_STATE, timezone_id=timezone)
    if not session_valid(context, settings):
        msg = 'SSO session expired. A human must run: uv run src/crosstab.py login'
        raise SystemExit(msg)
    return context


def doctor() -> int:
    ok = True
    try:
        settings = load_settings()
    except MissingSettingsError as e:
        print(f'settings   FAIL {e}; a human must run: uv run src/wizard.py')
        return 1
    print(
        f'settings   ok  server={settings.server} site={settings.site or "(default)"}'
    )
    try:
        jobs = load_jobs(Path('jobs.toml'))
        print(f'jobs.toml  ok  {len(jobs)} job(s): {", ".join(j.name for j in jobs)}')
    except (JobError, tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as e:
        ok = False
        print(f'jobs.toml  FAIL {e}')
    if not AUTH_STATE.exists():
        print(f'session    FAIL {AUTH_STATE} missing; a human must run login')
        return 1
    with sync_playwright() as pw:
        browser = launch_browser(pw, headless=True)
        context = browser.new_context(storage_state=AUTH_STATE)
        valid = session_valid(context, settings)
        browser.close()
    print(
        f'session    {"ok" if valid else "FAIL"}  {AUTH_STATE} (mode {AUTH_STATE.stat().st_mode & 0o777:o})'
    )
    return 0 if ok and valid else 1


def inspect(view: str) -> int:
    settings = load_settings()
    with sync_playwright() as pw:
        browser = launch_browser(pw, headless=True)
        page = open_view(_browser_context(browser, settings), settings, view)
        print(json.dumps(page.evaluate(INSPECT_JS), indent=2, ensure_ascii=False))
        browser.close()
    return 0


def date_filter(args: argparse.Namespace) -> int:
    """Apply one date range in several browser timezones; the applied range and the export must not move."""
    settings = load_settings()
    range_filter = RangeFilter(args.field, args.sheet, args.min, args.max)
    sent = filter_payload(range_filter)
    job = EmbedJob('date-filter', args.view, [args.sheet], [range_filter])
    results: list[dict[str, Any]] = []
    with sync_playwright() as pw:
        browser = launch_browser(pw, headless=True)
        for tz in TIMEZONES:
            context = _browser_context(browser, settings, tz)
            page = open_view(context, settings, args.view)
            page.evaluate(APPLY_JS, {'filters': [sent], 'params': {}})
            applied = page.evaluate(
                READ_RANGE_JS, {'sheet': args.sheet, 'field': args.field}
            )
            page.close()
            (csv,) = export_embed(
                context, settings, job, args.out / tz.replace('/', '_')
            )
            data = csv.read_bytes()
            results.append(
                {
                    'timezone': tz,
                    'applied': applied,
                    'csv': str(csv),
                    'csv_sha256': hashlib.sha256(data).hexdigest(),
                    'csv_lines': data.count(b'\n'),
                }
            )
            context.close()
        browser.close()

    def day(bound: dict[str, Any] | None) -> str | None:
        return str(bound['value'])[:10] if bound else None

    problems = []
    for r in results:
        got = (day(r['applied']['min']), day(r['applied']['max']))
        if got != (sent['min'], sent['max']):
            problems.append(
                f'{r["timezone"]}: applied {got}, asked for {(sent["min"], sent["max"])}'
            )
    if len({r['csv_sha256'] for r in results}) > 1:
        problems.append('exported CSVs differ between timezones')
    report = {
        'view': args.view,
        'sheet': args.sheet,
        'field': args.field,
        'asked': [args.min, args.max],
        'results': results,
        'problems': problems,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'date-filter.json').write_text(
        json.dumps(report, indent=2), encoding='utf-8'
    )
    for r in results:
        a = r['applied']
        print(
            f'{r["timezone"]:<20} browser={a["browser_tz"]:<20} min={a["min"]} max={a["max"]} '
            f'lines={r["csv_lines"]} sha={r["csv_sha256"][:12]}'
        )
    print('PASS' if not problems else 'FAIL\n  ' + '\n  '.join(problems))
    print(f'evidence: {args.out / "date-filter.json"}')
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor')
    commands.add_parser('inspect').add_argument('view')
    check = commands.add_parser('date-filter')
    check.add_argument('view')
    check.add_argument('--sheet', required=True)
    check.add_argument('--field', required=True)
    check.add_argument('--min', required=True)
    check.add_argument('--max', required=True)
    check.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    match args.command:
        case 'doctor':
            return doctor()
        case 'inspect':
            return inspect(args.view)
        case _:
            return date_filter(args)


if __name__ == '__main__':
    sys.exit(main())
