from collections.abc import Sequence
from pathlib import Path

import pytest

from tabpull import app
from tabpull.filters import format_filter, make_filter
from tabpull.jobs import (
    Job,
    JobError,
    RangeFilter,
    ValuesFilter,
    check_known,
    delete_jobs,
    job_to_toml,
    load_jobs,
    save_jobs,
    saved_jobs_or_exit,
    update_job,
)


def _job(
    name: str,
    *,
    sheets: Sequence[str] = ('A', 'B'),
    filters: Sequence[ValuesFilter | RangeFilter] = (),
    params: dict[str, str] | None = None,
) -> Job:
    return Job(
        name, 'Sales/Overview', list(sheets), 'finance', list(filters), params or {}
    )


def _write(path: Path, *jobs: Job) -> None:
    save_jobs(path, list(jobs))


def test_save_jobs_round_trips_filters_and_params(tmp_path: Path) -> None:
    path = tmp_path / 'nested' / 'jobs.toml'
    jobs = [
        _job(
            'daily',
            filters=[
                ValuesFilter('Region', ['West', 'Central'], 'A'),
                RangeFilter('Order Date', 'B', '2026-09-01', None),
            ],
            params={'Top N': '25'},
        ),
        _job('weekly'),
    ]

    save_jobs(path, jobs)

    assert load_jobs(path) == jobs
    assert not path.with_name('jobs.toml.tmp').exists()


def test_save_jobs_refuses_a_job_it_could_not_read_back(tmp_path: Path) -> None:
    path = tmp_path / 'jobs.toml'
    _write(path, _job('daily'))
    before = path.read_text(encoding='utf-8')

    with pytest.raises(JobError, match='needs at least one sheet'):
        save_jobs(path, [_job('daily', sheets=[])])

    assert path.read_text(encoding='utf-8') == before


def test_update_job_edits_in_place_and_keeps_order(tmp_path: Path) -> None:
    path = tmp_path / 'jobs.toml'
    _write(path, _job('a'), _job('b'), _job('c'))
    edited = _job('b', filters=[RangeFilter('Order Date', 'A', None, '2026-09-25')])

    update_job(path, 'b', edited)

    assert [job.name for job in load_jobs(path)] == ['a', 'b', 'c']
    assert load_jobs(path)[1] == edited


def test_update_job_renames_but_refuses_a_taken_name(tmp_path: Path) -> None:
    path = tmp_path / 'jobs.toml'
    _write(path, _job('a'), _job('b'))

    update_job(path, 'a', _job('renamed'))
    assert [job.name for job in load_jobs(path)] == ['renamed', 'b']

    with pytest.raises(JobError, match="named 'b' already exists"):
        update_job(path, 'renamed', _job('b'))
    with pytest.raises(JobError, match='Unknown jobs: gone'):
        update_job(path, 'gone', _job('gone'))


def test_delete_jobs_keeps_the_rest_and_names_unknown_jobs(tmp_path: Path) -> None:
    path = tmp_path / 'jobs.toml'
    _write(path, _job('a'), _job('b'), _job('c'))

    kept = delete_jobs(path, ['a', 'c'])

    assert [job.name for job in kept] == ['b']
    assert load_jobs(path) == kept
    with pytest.raises(JobError, match=r'Unknown jobs: x\. Saved jobs: b'):
        delete_jobs(path, ['b', 'x'])
    assert [job.name for job in load_jobs(path)] == ['b']

    delete_jobs(path, ['b'])
    assert load_jobs(path) == []


def test_saved_jobs_or_exit_names_the_file_for_unreadable_and_empty(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'jobs.toml'
    with pytest.raises(SystemExit, match=r'No jobs in .*jobs\.toml\. Run: tabpull add'):
        saved_jobs_or_exit(path)
    path.write_text('job = 1\n', encoding='utf-8')
    with pytest.raises(SystemExit, match=r'jobs\.toml: `job` must be an array'):
        saved_jobs_or_exit(path)
    _write(path, _job('a'))
    assert [job.name for job in saved_jobs_or_exit(path)] == ['a']


def test_check_known_lists_unknown_names_sorted_with_the_saved_ones() -> None:
    jobs = [_job('b'), _job('a')]
    check_known(['a'], jobs)
    check_known([], jobs)
    with pytest.raises(JobError, match=r'^Unknown jobs: x, y\. Saved jobs: b, a$'):
        check_known(['y', 'a', 'x'], jobs)
    with pytest.raises(JobError, match=r'Saved jobs: none$'):
        check_known(['x'], [])


def test_bare_tabpull_opens_the_tui_only_at_a_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opened: list[tuple[Path, Path]] = []
    monkeypatch.setattr('tabpull.tui.run_tui', lambda *args: opened.append(args))
    monkeypatch.setattr(app.ui, 'interactive', lambda: True)
    path = tmp_path / 'jobs.toml'

    assert app.main(['--jobs', str(path), '--out', 'x']) == 0

    assert opened == [(path, Path('x'))]


def test_make_filter_builds_and_validates_values_and_ranges() -> None:
    assert make_filter(' Region ', 'A', values='West| Central') == ValuesFilter(
        ' Region ', ['West', 'Central'], 'A'
    )
    assert make_filter('Order Date', '', low=' 9/1/2026 ', high='') == RangeFilter(
        'Order Date', '', '9/1/2026', None
    )
    with pytest.raises(JobError, match='needs a min, a max, or both'):
        make_filter('Order Date', 'A', low=' ', high='')
    with pytest.raises(JobError, match='not a real date'):
        make_filter('Order Date', 'A', low='2026-02-31')
    with pytest.raises(JobError, match='relative date'):
        make_filter('Order Date', 'A', high='yesterday')
    with pytest.raises(JobError, match='field name'):
        make_filter(' ', 'A', values='x')


def test_filter_types_share_kind_shown_pick_list_and_bounds() -> None:
    values = ValuesFilter('Region', ['West', 'East'], 'A')
    both = RangeFilter('Order Date', 'A', '2026-09-01', '2026-09-25')
    open_min = RangeFilter('Order Date', 'A', None, '2026-09-25')

    assert (values.kind, values.shown, values.pick_list, values.bounds) == (
        'values',
        'West|East',
        'West|East',
        ('', ''),
    )
    assert (both.kind, both.shown, both.pick_list) == (
        'range',
        '2026-09-01..2026-09-25',
        '',
    )
    assert (open_min.shown, open_min.bounds) == ('..2026-09-25', ('', '2026-09-25'))
    assert 'kind' not in job_to_toml(_job('a', filters=[values, both]))


def test_format_filter_reads_like_the_flag_syntax() -> None:
    assert format_filter(ValuesFilter('Region', ['West', 'East'], 'A')) == (
        'Region=West|East @A'
    )
    assert format_filter(RangeFilter('Order Date', '', None, '2026-09-25')) == (
        'Order Date=..2026-09-25'
    )
