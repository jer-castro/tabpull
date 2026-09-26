import tomllib

import pytest

import crosstab
from crosstab import (
    EmbedJob,
    JobError,
    RangeFilter,
    RestJob,
    ValuesFilter,
    job_to_toml,
    match_score,
    normalize_csv,
    parse_job,
    rest_filter_value,
)
from tableau import parse_tableau_url


def test_normalize_csv_rewrites_tableau_utf16_tsv_as_utf8_csv() -> None:
    raw = 'Region\tSales, total\nWest\t"1,200"\nSão Paulo\t5\n'.encode('utf-16')

    assert normalize_csv(raw) == 'Region,"Sales, total"\nWest,"1,200"\nSão Paulo,5\n'


def test_normalize_csv_passes_utf8_through_without_bom() -> None:
    assert normalize_csv('\ufeffa,b\n1,2\n'.encode()) == 'a,b\n1,2\n'


@pytest.mark.parametrize(
    ('url', 'server', 'site', 'view'),
    [
        (
            'https://prod-useast-a.online.tableau.com/#/site/demo/views/Sales/Overview?:iid=1',
            'https://prod-useast-a.online.tableau.com',
            'demo',
            'Sales/Overview',
        ),
        (
            'https://tableau.corp.com/t/finance/views/Budget/Dash',
            'https://tableau.corp.com',
            'finance',
            'Budget/Dash',
        ),
        (
            'https://tableau.corp.com/#/views/Budget/Dash',
            'https://tableau.corp.com',
            '',
            'Budget/Dash',
        ),
        (
            'https://tableau.corp.com/#/site/finance/home',
            'https://tableau.corp.com',
            'finance',
            None,
        ),
    ],
)
def test_parse_tableau_url(url: str, server: str, site: str, view: str | None) -> None:
    parsed = parse_tableau_url(url)

    assert (parsed.server, parsed.site, parsed.view) == (server, site, view)


def test_jobs_round_trip_through_toml() -> None:
    jobs = [
        EmbedJob(
            'west "q1"',
            'Sales/Overview',
            ['Detail Table', 'Totals'],
            [
                ValuesFilter('Region', ['West', 'East'], 'Totals'),
                RangeFilter('Order Date', 'Detail Table', '2026-01-01', None),
            ],
            {'Top N': '10'},
        ),
        RestJob(
            'published', 'Sales/Detail', 'abc-123', [ValuesFilter('Region', ['West'])]
        ),
    ]
    text = '\n'.join(job_to_toml(job) for job in jobs)

    assert [parse_job(raw) for raw in tomllib.loads(text)['job']] == jobs


def test_rest_filter_value_escapes_commas_inside_values() -> None:
    assert rest_filter_value(['Smith, Jane', 'West']) == 'Smith\\, Jane,West'


@pytest.mark.parametrize(
    ('query', 'matches'),
    [
        ('sales overview', True),
        ('Dashbord', True),
        ('overview sales', True),
        ('inventory', False),
        ('sales inventory', False),
    ],
)
def test_match_score_finds_views_by_words_and_typos(query: str, matches: bool) -> None:
    text = 'Sales Dashboard SalesWorkbook/sheets/Overview'

    assert (match_score(query, text) >= crosstab.MIN_MATCH_SCORE) is matches


def test_embed_filters_default_to_first_sheet() -> None:
    job = parse_job(
        {
            'name': 'j',
            'view': 'W/V',
            'sheets': ['A', 'B'],
            'filters': [{'field': 'Region', 'values': ['West']}],
        }
    )

    assert job.filters == [ValuesFilter('Region', ['West'], 'A')]


@pytest.mark.parametrize(
    'raw',
    [
        {
            'name': 'j',
            'method': 'rest',
            'view': 'W/V',
            'view_id': 'x',
            'filters': [{'field': 'Date', 'min': '2026-01-01'}],
        },
        {
            'name': 'j',
            'method': 'rest',
            'view': 'W/V',
            'view_id': 'x',
            'filters': [
                {'field': 'Region', 'values': ['West', 'East']},
                {'field': 'Year', 'values': ['2024']},
            ],
        },
        {'name': 'j', 'view': 'W/V', 'sheets': []},
        {'name': 'j', 'method': 'ftp', 'view': 'W/V'},
        {'name': 'j', 'view': 'W/V', 'sheets': ['A'], 'typo': 1},
    ],
)
def test_parse_job_rejects_invalid_jobs(raw: dict[str, object]) -> None:
    with pytest.raises(JobError, match="job 'j'"):
        parse_job(raw)
