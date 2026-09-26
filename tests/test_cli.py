import runpy
import sys
import tomllib
from pathlib import Path
from typing import Any, Self, cast

import pytest

import crosstab
import tableau
import wizard
from crosstab import Job, JobError
from tableau import Settings, UnknownSiteError


def _xdg(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(root / 'config'))
    monkeypatch.setenv('XDG_DATA_HOME', str(root / 'data'))


def _site_values(
    server: str = 'https://tableau.example',
    site: str = 'finance',
    pat_name: str = 'tabpull',
    value: str = 'pat-value',
) -> dict[str, str]:
    return {
        'TABLEAU_SERVER_URL': server,
        'TABLEAU_SITE': site,
        'TABLEAU_PAT_NAME': pat_name,
        'TABLEAU_PAT_SECRET': value,
    }


def _settings(name: str, root: Path, value: str = 'pat-value') -> Settings:
    return Settings(
        'https://tableau.example',
        name,
        'tabpull',
        name,
        root / f'{name}.json',
        value,
    )


class _Playwright:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _notty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)


def test_linux_paths_follow_xdg_and_ignore_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.chdir(work)
    _xdg(monkeypatch, tmp_path)

    assert tableau.config_dir() == tmp_path / 'config' / 'tabpull'
    assert tableau.data_dir() == tmp_path / 'data' / 'tabpull'
    assert tableau.jobs_path() == tmp_path / 'config' / 'tabpull' / 'jobs.toml'
    assert tableau.exports_dir() == tmp_path / 'data' / 'tabpull' / 'exports'
    assert tableau.jobs_path() != work / 'jobs.toml'
    assert tableau.exports_dir() != work / 'exports'


def test_os_paths_when_xdg_is_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    monkeypatch.delenv('XDG_DATA_HOME', raising=False)
    monkeypatch.setattr(tableau.Path, 'home', lambda: tmp_path)

    monkeypatch.setattr(tableau.sys, 'platform', 'linux')
    assert tableau.config_dir() == tmp_path / '.config' / 'tabpull'
    assert tableau.data_dir() == tmp_path / '.local' / 'share' / 'tabpull'

    monkeypatch.setattr(tableau.sys, 'platform', 'darwin')
    assert (
        tableau.config_dir() == tmp_path / 'Library' / 'Application Support' / 'tabpull'
    )
    assert tableau.data_dir() == tableau.config_dir()

    monkeypatch.setattr(tableau.sys, 'platform', 'win32')
    monkeypatch.setenv('APPDATA', str(tmp_path / 'Roaming'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'Local'))
    assert tableau.config_dir() == tmp_path / 'Roaming' / 'tabpull'
    assert tableau.data_dir() == tmp_path / 'Local' / 'tabpull'

    monkeypatch.setattr(tableau.sys, 'platform', 'darwin')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg-config'))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'xdg-data'))
    assert tableau.config_dir() == tmp_path / 'xdg-config' / 'tabpull'
    assert tableau.data_dir() == tmp_path / 'xdg-data' / 'tabpull'


def test_sites_keep_separate_tokens_and_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.chdir(work)
    (work / '.env').write_text(
        'TABLEAU_SERVER_URL=https://from-dotenv\nTABLEAU_PAT_SECRET=from-dotenv\n',
        encoding='utf-8',
    )
    monkeypatch.setenv('TABLEAU_PAT_SECRET', 'from-env')
    _xdg(monkeypatch, tmp_path)
    alpha_value = 'alpha-token'
    beta_value = 'beta-token'
    tableau.save_site(
        'alpha', _site_values(server='https://a.example', value=alpha_value)
    )
    tableau.save_site(
        'beta', _site_values(server='https://b.example', site='', value=beta_value)
    )

    alpha = tableau.load_site('alpha')
    beta = tableau.load_site('beta')

    assert tableau.list_sites() == ['alpha', 'beta']
    assert alpha.server == 'https://a.example'
    assert alpha.pat_secret == alpha_value
    assert not beta.site
    assert beta.pat_secret == beta_value
    assert alpha.auth_path != beta.auth_path
    assert alpha.auth_path == tmp_path / 'data' / 'tabpull' / 'auth' / 'alpha.json'
    assert beta.auth_path.parent == alpha.auth_path.parent
    assert tableau.site_env_path('alpha').stat().st_mode & 0o777 == 0o600
    assert 'from-dotenv' not in tableau.site_env_path('alpha').read_text(
        encoding='utf-8'
    )
    assert 'alpha-token' not in repr(alpha)


def test_help_lists_the_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        crosstab.main(['--help'])
    text = capsys.readouterr().out

    assert exc.value.code == 0
    for name in ('setup', 'add', 'run', 'login', '--jobs', '--out'):
        assert name in text


def test_add_help_lists_noninteractive_flags(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        crosstab.main(['add', '--help'])
    text = capsys.readouterr().out

    assert exc.value.code == 0
    for flag in ('--site', '--view', '--sheet', '--filter', '--param', '--name'):
        assert flag in text


def test_setup_subcommand_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str | None] = {}

    def fake(site_name: str | None = None) -> str:
        seen['site'] = site_name
        return site_name or 'created'

    with pytest.raises(SystemExit, match='Site name'):
        crosstab.main(['setup', '--site', '../nope'])

    monkeypatch.setattr(crosstab.wizard, 'main', fake)

    assert crosstab.main(['setup', '--site', 'finance']) == 0
    assert seen['site'] == 'finance'


def test_add_flags_write_a_job_without_prompts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.chdir(work)
    _xdg(monkeypatch, tmp_path)
    tableau.save_site('demo', _site_values())
    tableau.save_site('other', _site_values(server='https://other.example'))
    jobs = tmp_path / 'scratch.toml'

    def refuse_prompt(prompt: str = '') -> str:
        msg = f'prompted: {prompt}'
        raise AssertionError(msg)

    monkeypatch.setattr('builtins.input', refuse_prompt)
    code = crosstab.main(
        [
            '--jobs',
            str(jobs),
            'add',
            '--site',
            'demo',
            '--view',
            'https://tableau.example/#/site/finance/views/Sales/Overview',
            '--sheet',
            'Order Detail',
            '--sheet',
            'Totals',
            '--filter',
            'Region=West|Central',
            '--filter',
            'Order Date=2026-09-01..2026-09-25 @Totals',
            '--param',
            'Top N=25',
            '--name',
            'daily-west',
        ]
    )
    job = crosstab.parse_job(tomllib.loads(jobs.read_text(encoding='utf-8'))['job'][0])
    out = capsys.readouterr().out

    assert code == 0
    assert job.name == 'daily-west'
    assert job.site == 'demo'
    assert job.view == 'Sales/Overview'
    assert job.sheets == ['Order Detail', 'Totals']
    assert job.params == {'Top N': '25'}
    assert job.filters[0] == crosstab.ValuesFilter(
        'Region', ['West', 'Central'], 'Order Detail'
    )
    assert job.filters[1] == crosstab.RangeFilter(
        'Order Date', 'Totals', '2026-09-01', '2026-09-25'
    )
    assert "filter 'Region' names no sheet" in out
    assert 'Order Detail' in out
    assert 'pat-value' not in out
    assert 'https://tableau.example' in out
    assert not tableau.jobs_path().exists()
    assert not (work / 'jobs.toml').exists()
    assert not (work / 'exports').exists()
    with pytest.raises(SystemExit, match='already exists'):
        crosstab.main(
            [
                '--jobs',
                str(jobs),
                'add',
                '--site',
                'demo',
                '--view',
                'Sales/Overview',
                '--sheet',
                'Order Detail',
                '--name',
                'daily-west',
            ]
        )


def test_add_flags_imply_the_only_site_and_default_jobs_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    _xdg(monkeypatch, tmp_path)
    tableau.save_site('demo', _site_values())
    monkeypatch.setattr(
        'builtins.input',
        lambda prompt='': (_ for _ in ()).throw(AssertionError(prompt)),
    )

    assert (
        crosstab.main(
            ['add', '--view', 'Sales/Overview', '--sheet', 'Detail', '--name', 'j']
        )
        == 0
    )
    jobs = crosstab.load_jobs(tableau.jobs_path())

    assert [job.name for job in jobs] == ['j']
    assert jobs[0].site == 'demo'
    assert not (tmp_path / 'jobs.toml').exists()


def test_add_without_flags_still_prompts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _xdg(monkeypatch, tmp_path)
    tableau.save_site('demo', _site_values())
    seen: list[str] = []
    monkeypatch.setattr(
        crosstab,
        'add_job',
        lambda settings, path, name=None: seen.append(settings.name),
    )
    monkeypatch.setattr(
        crosstab,
        'add_job_from_flags',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('flags')),
    )

    assert crosstab.main(['add']) == 0
    assert seen == ['demo']
    with pytest.raises(SystemExit, match='--view'):
        crosstab.main(['add', '--sheet', 'A'])
    with pytest.raises(SystemExit, match='--sheet'):
        crosstab.main(['add', '--view', 'Sales/Overview', '--filter', 'Region=West'])


def test_login_and_run_name_each_site_without_printing_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _xdg(monkeypatch, tmp_path)
    hidden = 'do-not-print'
    tableau.save_site('demo', _site_values(value=hidden))
    monkeypatch.setattr(crosstab, 'sync_playwright', _Playwright)
    monkeypatch.setattr(crosstab, 'sso_login', lambda _pw, _settings: None)

    assert crosstab.main(['login']) == 0
    login_out = capsys.readouterr().out
    assert 'https://tableau.example' in login_out
    assert 'local name demo' in login_out
    assert hidden not in login_out

    jobs = tmp_path / 'jobs.toml'
    out = tmp_path / 'out'
    jobs.write_text(
        """
[[job]]
name = "a"
site = "one"
view = "W/V"
sheets = ["S"]

[[job]]
name = "b"
site = "one"
view = "W/V"
sheets = ["S"]

[[job]]
name = "c"
site = "two"
view = "W/V"
sheets = ["S"]

[[job]]
name = "bad"
site = "two"
view = "W/V"
sheets = ["S"]

[[job]]
name = "gone-job"
site = "gone"
view = "W/V"
sheets = ["S"]
""",
        encoding='utf-8',
    )
    opened: list[str] = []

    def load_site(name: str) -> Settings:
        if name == 'gone':
            msg = "No site named 'gone'."
            raise UnknownSiteError(msg)
        return _settings(name, tmp_path, value=hidden)

    def export_embed(
        _context: object, settings: Settings, job: Job, out_dir: Path
    ) -> list[Path]:
        if job.name == 'bad':
            msg = 'broken view'
            raise JobError(msg)
        return [out_dir / f'{settings.name}-{job.name}.csv']

    monkeypatch.setattr(crosstab, 'load_site', load_site)
    monkeypatch.setattr(
        crosstab, 'browser_session', lambda _pw, settings: opened.append(settings.name)
    )
    monkeypatch.setattr(crosstab, 'export_embed', export_embed)

    code = crosstab.main(['--jobs', str(jobs), '--out', str(out), 'run'])
    run_out = capsys.readouterr().out

    assert code == 1
    assert opened == ['one', 'two']
    assert '✓ a:' in run_out
    assert '✓ b:' in run_out
    assert '✓ c:' in run_out
    assert '✗ bad: broken view' in run_out
    assert '✗ gone-job: No site named' in run_out
    assert hidden not in run_out
    assert f'Exporting 5 job(s) to {out}/' in run_out


def test_run_defaults_the_output_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _xdg(monkeypatch, tmp_path)
    jobs = tmp_path / 'jobs.toml'
    jobs.write_text(
        """
[[job]]
name = "a"
site = "one"
view = "W/V"
sheets = ["S"]
""",
        encoding='utf-8',
    )
    seen: list[Path] = []
    monkeypatch.setattr(crosstab, 'sync_playwright', _Playwright)
    monkeypatch.setattr(crosstab, 'load_site', lambda name: _settings(name, tmp_path))
    monkeypatch.setattr(crosstab, 'browser_session', lambda _pw, _settings: object())
    monkeypatch.setattr(
        crosstab,
        'export_embed',
        lambda _context, _settings, _job, out_dir: (
            seen.append(out_dir) or [out_dir / 'a.csv']
        ),
    )

    assert crosstab.main(['--jobs', str(jobs), 'run', 'a']) == 0
    assert seen == [tableau.exports_dir()]


def test_several_sites_require_a_flag_when_there_is_no_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _xdg(monkeypatch, tmp_path)
    _notty(monkeypatch)
    tableau.save_site('alpha', _site_values())
    tableau.save_site('beta', _site_values(server='https://b.example'))

    with pytest.raises(SystemExit, match='--site'):
        crosstab.main(['login'])
    with pytest.raises(SystemExit, match='--site'):
        crosstab.main(['add', '--view', 'Sales/Overview', '--sheet', 'Detail'])


def test_missing_session_names_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _notty(monkeypatch)
    monkeypatch.setattr(tableau, 'launch_browser', lambda _pw, *, headless: object())
    settings = _settings('demo', tmp_path)

    with pytest.raises(SystemExit, match=r'tabpull login --site demo'):
        tableau.browser_session(cast('Any', object()), settings)


def test_setup_writes_each_site_under_xdg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.chdir(work)
    (work / '.env').write_text('TABLEAU_PAT_SECRET=from-dotenv\n', encoding='utf-8')
    _xdg(monkeypatch, tmp_path)
    answers = iter(
        [
            '',
            'https://tableau.example.com/#/site/finance/home',
            'y',
            'tabpull',
            'n',
            '',
            '',
            '',
            'n',
            '',
            'https://other.example/t/ops/views/Book/Dash',
            'y',
            'other-token',
            'n',
        ]
    )
    prompts: list[str] = []

    def fake_input(prompt: str = '') -> str:
        prompts.append(prompt)
        try:
            return next(answers)
        except StopIteration as e:
            msg = '\n'.join(prompts)
            raise AssertionError(msg) from e

    first_value = 'super-secret-value'
    third_value = 'other-secret'
    hidden = iter([first_value, '', third_value])
    monkeypatch.setattr('builtins.input', fake_input)
    monkeypatch.setattr(
        wizard.getpass, 'getpass', lambda prompt='', stream=None: next(hidden)
    )
    monkeypatch.setattr(wizard, 'open_url', lambda _url: None)
    monkeypatch.setattr(wizard, 'rest_session', lambda _settings: _Playwright())
    monkeypatch.setattr(
        wizard,
        'sync_playwright',
        lambda: (_ for _ in ()).throw(AssertionError('browser')),
    )

    assert wizard.main('finance') == 'finance'
    assert wizard.main('finance') == 'finance'
    assert wizard.main('ops') == 'ops'
    text = capsys.readouterr().out
    finance = tableau.load_site('finance')
    ops = tableau.load_site('ops')

    assert finance.server == 'https://tableau.example.com'
    assert finance.site == 'finance'
    assert finance.pat_secret == first_value
    assert ops.server == 'https://other.example'
    assert ops.site == 'ops'
    assert ops.view_url('Book/Dash') == 'https://other.example/t/ops/views/Book/Dash'
    assert ops.pat_secret == third_value
    assert finance.auth_path != ops.auth_path
    assert not finance.auth_path.exists()
    assert first_value not in text
    assert third_value not in text
    assert 'tabpull add --site finance' in text
    assert str(tableau.jobs_path()) in text
    assert str(tableau.exports_dir()) in text
    assert first_value not in (work / '.env').read_text(encoding='utf-8')


def test_source_files_point_at_the_command(capsys: pytest.CaptureFixture[str]) -> None:
    root = Path(__file__).resolve().parents[1]
    for name in ('crosstab.py', 'wizard.py'):
        with pytest.raises(SystemExit) as exc:
            runpy.run_path(str(root / 'src' / name), run_name='__main__')
        assert exc.value.code == 2
        assert 'tabpull' in capsys.readouterr().err
