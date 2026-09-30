"""Export Tableau crosstab CSVs for the dashboard sheets you name.

Commands:
  setup  save a site's personal access token and SSO session
  add    record a job (prompts, or flags for the site, view, sheets, and filters)
  remove delete saved jobs from the jobs file
  run    export jobs from the jobs file into the current folder
  login  refresh a site's SSO session

Run tabpull with no command at a terminal to open the interactive screen.
Piped, it prints configured sites and saved jobs.
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
from tabpull.filters import (
    filters_for_run,
    params_for_run,
    parse_filter_spec,
    parse_param_spec,
)
from tabpull.home import file_flags, show_home
from tabpull.jobs import JobError, check_known, saved_jobs_or_exit
from tabpull.remove import remove_jobs
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
    flag_mode = args.view or args.sheets or args.filter_specs or args.params
    try:
        if flag_mode or not ui.interactive():
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


def _cmd_run(args: argparse.Namespace, jobs_file: Path, out_dir: Path) -> int:
    jobs = saved_jobs_or_exit(jobs_file)
    selected = [job for job in jobs if not args.names or job.name in args.names]
    try:
        check_known(args.names, jobs)
        overrides = [parse_filter_spec(spec) for spec in args.filter_specs or []]
        param_overrides = [parse_param_spec(spec) for spec in args.params or []]
        selected = [
            params_for_run(filters_for_run(job, overrides), param_overrides)
            for job in selected
        ]
    except JobError as e:
        raise SystemExit(str(e)) from e
    report = open_report(selected, out_dir)
    try:
        failed = run_jobs(selected, out_dir, report, parallel=args.parallel)
    except JobError as e:
        raise SystemExit(str(e)) from e
    filter_flags = [
        flag for spec in args.filter_specs or [] for flag in ('--filter', spec)
    ]
    param_flags = [flag for spec in args.params or [] for flag in ('--param', spec)]
    parallel_flags = ['--parallel', str(args.parallel)] if args.parallel != 1 else []
    rerun = shlex.join(
        [
            'tabpull',
            'run',
            *file_flags(args),
            *parallel_flags,
            *filter_flags,
            *param_flags,
            '--',
            *failed,
        ]
    )
    report.summary(
        len(selected) - len(failed), len(selected), rerun if failed else None
    )
    return 1 if failed else 0


class _Parser(argparse.ArgumentParser):
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


def _at_least_one(text: str) -> int:
    try:
        value = int(text)
    except ValueError as e:
        msg = f'parallel must be an integer, not {text!r}'
        raise argparse.ArgumentTypeError(msg) from e
    if value < 1:
        msg = 'parallel must be at least 1'
        raise argparse.ArgumentTypeError(msg)
    return value


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
    remove = commands.add_parser(
        'remove',
        help='delete saved jobs from the jobs file',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  tabpull remove daily-west
  tabpull remove daily-west weekly-east
  tabpull remove

With no names, a terminal asks which jobs to remove, then confirms.
Otherwise pass the names. The jobs file is rewritten from the jobs that
remain. Removing every job leaves that file empty.
""",
    )
    remove.add_argument(
        'names',
        nargs='*',
        help='jobs to delete (prompt when omitted on a terminal)',
    )
    _add_file_flags(remove, out=False, default=argparse.SUPPRESS)
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

--param uses the same Name=value syntax as add. It applies to every job in
this run and does not rewrite the jobs file. A saved param of that name is
replaced. A name the job does not have is added for this run.

examples:
  tabpull run
  tabpull run daily-west
  tabpull run daily-west --filter "Order Date=2026-09-01.." --out ~/reports
  tabpull run daily-west weekly-east --filter "Order Date=2026-09-01..2026-09-07"
  tabpull run daily-west --param "Top N=10"
  tabpull run --parallel 4
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
    run.add_argument(
        '--param',
        action='append',
        dest='params',
        metavar='NAME=VALUE',
        help=(
            'override Name=value for this run only '
            '(repeatable; applied to every selected job; jobs file unchanged)'
        ),
    )
    run.add_argument(
        '--parallel',
        type=_at_least_one,
        default=1,
        help='how many jobs to export at once (default: 1)',
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
        case 'remove':
            remove_jobs(jobs_file, args.names)
        case 'run':
            return _cmd_run(args, jobs_file, out_dir)
        case _ if ui.interactive():
            from tabpull.tui import run_tui  # noqa: PLC0415

            run_tui(jobs_file, out_dir)
        case _:
            show_home(args, jobs_file, out_dir)
    return 0


def cli() -> None:
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
    print('Run: tabpull setup | add | remove | run | login', file=sys.stderr)
    sys.exit(2)
