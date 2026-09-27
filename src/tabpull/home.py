import argparse
import json
import re
import shlex
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

from rich import box
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from tabpull import ui
from tabpull.jobs import Job, JobError, load_jobs
from tabpull.tableau import MissingSettingsError, list_sites, load_site

_TOON_NUMBER = re.compile(r'^[+-]?[0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?$', re.IGNORECASE)
_TOON_QUOTE = re.compile(r'[,:"\\\[\]{}\x00-\x1f]')


def _toon(value: object) -> str:
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


def file_flags(args: argparse.Namespace) -> list[str]:
    flags = []
    if args.jobs:
        flags += ['--jobs', str(args.jobs)]
    if args.out:
        flags += ['--out', str(args.out)]
    return flags


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
    quoted = shlex.join(file_flags(args))
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
        f'  [dim]jobs file[/] {escape(ui.home_path(jobs_file))}\n'
        f'  [dim]exports  [/] {escape(ui.home_path(out_dir))}/<job>/<sheet>.csv'
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


def show_home(args: argparse.Namespace, jobs_file: Path, out_dir: Path) -> None:
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
        f'bin: {_toon(ui.home_path(sys.argv[0]))}',
        'description: Export Tableau dashboard sheet crosstabs to CSV',
        f'jobs_file: {_toon(ui.home_path(jobs_file))}',
        f'out: {_toon(ui.home_path(out_dir))}',
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
