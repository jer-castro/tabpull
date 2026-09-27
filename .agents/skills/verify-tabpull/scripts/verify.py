#!/usr/bin/env -S uv run python

import argparse
import hashlib
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'src'))

from playwright.sync_api import Browser, BrowserContext, sync_playwright

from tabpull.embed import APPLY_JS, INSPECT_JS, export_embed, filter_payload, open_view
from tabpull.jobs import Job, JobError, RangeFilter, load_jobs
from tabpull.tableau import (
    MissingSettingsError,
    Settings,
    jobs_path,
    launch_browser,
    list_sites,
    load_site,
    session_valid,
)

TIMEZONES = ('UTC', 'America/Los_Angeles', 'Pacific/Auckland')

READ_RANGE_JS = """async ({ sheet, field }) => {
  const active = window.viz.workbook.activeSheet;
  const worksheets = active.sheetType === 'worksheet' ? [active] : active.worksheets;
  const ws = worksheets.find((w) => w.name === sheet);
  const f = (await ws.getFiltersAsync()).find((x) => x.fieldName === field);
  if (!f) throw new Error('No filter on ' + field + ' after applying it');
  const out = (v) => v && { value: v.value instanceof Date ? v.value.toISOString() : v.value, formatted: v.formattedValue };
  const domain = await f.getDomainAsync(window.tableauEnums.FilterDomainType.Database);
  return { type: f.filterType, min: out(f.minValue), max: out(f.maxValue),
           domain: { min: out(domain.min), max: out(domain.max) },
           browser_tz: Intl.DateTimeFormat().resolvedOptions().timeZone };
}"""


def _settings(site: str | None) -> Settings:
    if site:
        return load_site(site)
    names = list_sites()
    if len(names) == 1:
        return load_site(names[0])
    listed = ', '.join(names) or '(none)'
    msg = f'Pass --site. Configured sites: {listed}'
    raise SystemExit(msg)


def _browser_context(
    browser: Browser, settings: Settings, timezone: str = 'UTC'
) -> BrowserContext:
    context = browser.new_context(
        storage_state=settings.auth_path, timezone_id=timezone
    )
    if not session_valid(context, settings):
        msg = (
            f'SSO session for site {settings.name!r} is missing or expired. '
            f'A human must run: tabpull login --site {settings.name}'
        )
        raise SystemExit(msg)
    return context


def doctor() -> int:
    names = list_sites()
    if not names:
        print('settings   FAIL no sites configured; a human must run: tabpull setup')
        return 1
    ok = True
    for name in names:
        try:
            settings = load_site(name)
        except MissingSettingsError as e:
            ok = False
            print(f'settings   FAIL {name}: {e}')
            continue
        print(
            f'settings   ok  {name} server={settings.server} '
            f'site={settings.site or "(default)"}'
        )
        auth = settings.auth_path
        if not auth.exists():
            ok = False
            print(
                f'session    FAIL {name} {auth} missing; a human must run: '
                f'tabpull login --site {name}'
            )
            continue
        with sync_playwright() as pw:
            browser = launch_browser(pw, headless=True)
            context = browser.new_context(storage_state=auth)
            valid = session_valid(context, settings)
            browser.close()
        print(
            f'session    {"ok" if valid else "FAIL"}  {name} {auth} '
            f'(mode {auth.stat().st_mode & 0o777:o})'
        )
        ok = ok and valid
    jobs_file = jobs_path()
    try:
        jobs = load_jobs(jobs_file)
        print(
            f'jobs       ok  {jobs_file} {len(jobs)} job(s): '
            f'{", ".join(job.name for job in jobs)}'
        )
    except (JobError, tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as e:
        ok = False
        print(f'jobs       FAIL {jobs_file}: {e}')
    return 0 if ok else 1


def inspect(view: str, site: str | None) -> int:
    settings = _settings(site)
    with sync_playwright() as pw:
        browser = launch_browser(pw, headless=True)
        page = open_view(_browser_context(browser, settings), settings, view)
        print(json.dumps(page.evaluate(INSPECT_JS), indent=2, ensure_ascii=False))
        browser.close()
    return 0


def _bound_day(bound: dict[str, Any] | None) -> str | None:
    return str(bound['value'])[:10] if bound else None


def _date_problems(results: list[dict[str, Any]], sent: dict[str, Any]) -> list[str]:
    problems = []
    for result in results:
        applied = result['applied']
        for side in ('min', 'max'):
            got = _bound_day(applied[side])
            want = sent[side] or _bound_day(applied['domain'][side])
            if got is None or got != want:
                what = 'asked for' if sent[side] else 'filter endpoint'
                problems.append(
                    f'{result["timezone"]}: applied {side} {got}, {what} {want}'
                )
    for side in ('min', 'max'):
        ends = {_bound_day(result['applied'][side]) for result in results}
        if not sent[side] and len(ends) > 1:
            problems.append(
                f'open {side} differs between timezones: {sorted(ends, key=str)}'
            )
    if len({result['csv_sha256'] for result in results}) > 1:
        problems.append('exported CSVs differ between timezones')
    return problems


def date_filter(args: argparse.Namespace) -> int:
    settings = _settings(args.site)
    range_filter = RangeFilter(args.field, args.sheet, args.min, args.max)
    sent = filter_payload(range_filter)
    job = Job('date-filter', args.view, [args.sheet], settings.name, [range_filter])
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

    problems = _date_problems(results, sent)
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
    parser.add_argument(
        '--site', help='local site name when more than one site is configured'
    )
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor')
    commands.add_parser('inspect').add_argument('view')
    check = commands.add_parser('date-filter')
    check.add_argument('view')
    check.add_argument('--sheet', required=True)
    check.add_argument('--field', required=True)
    check.add_argument(
        '--min', help='YYYY-MM-DD or M/D/YYYY; omit for the filter minimum'
    )
    check.add_argument(
        '--max', help='YYYY-MM-DD or M/D/YYYY; omit for the filter maximum'
    )
    check.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    match args.command:
        case 'doctor':
            return doctor()
        case 'inspect':
            return inspect(args.view, args.site)
        case _:
            return date_filter(args)


if __name__ == '__main__':
    sys.exit(main())
