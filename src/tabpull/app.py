"""Export Tableau crosstab CSVs for the dashboard sheets you name.

Commands:
  setup  save a site's personal access token and SSO session
  add    record a job (prompts, or flags for the site, view, sheets, and filters)
  run    export jobs from the jobs file into the current folder
  login  refresh a site's SSO session

Run tabpull with no command to see configured sites and saved jobs.
"""

import argparse
import shlex
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

import questionary
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from rich.rule import Rule

from tabpull import ui, wizard
from tabpull.add import add_job, add_job_from_flags
from tabpull.cli import VERSION_FLAGS, version
from tabpull.filters import filters_for_run, parse_filter_spec
from tabpull.home import file_flags, show_home
from tabpull.jobs import JobError, load_jobs
from tabpull.run import open_report, run_jobs
from tabpull.tableau import (
    MissingSettingsError,
    Settings,
    UnknownSiteError,
    check_site_name,
    jobs_path,
    list_sites,
    load_site,
    sso_login,
)


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


def _run_flags(args: argparse.Namespace) -> list[str]:
    flags = file_flags(args)
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
    report = open_report(selected, out_dir)
    failed = run_jobs(selected, out_dir, report)
    # Flags before `--` so a job named like an option (`-daily`, `-h`) stays a name.
    rerun = shlex.join(['tabpull', 'run', *_run_flags(args), '--', *failed])
    report.summary(
        len(selected) - len(failed), len(selected), rerun if failed else None
    )
    return 1 if failed else 0


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
            show_home(args, jobs_file, out_dir)
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
