import json
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path


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
    name: str
    view: str
    sheets: list[str]
    site: str
    filters: list[ValuesFilter | RangeFilter] = field(default_factory=list)
    params: dict[str, str] = field(default_factory=dict)


class JobError(Exception):
    pass


def _parse_filter(raw: object) -> ValuesFilter | RangeFilter:
    if not isinstance(raw, dict):
        msg = f'filter {raw!r} is not a table'
        raise JobError(msg)
    raw = dict(raw)
    raw['sheet'] = str(raw.get('sheet') or '').strip()
    if 'values' not in raw:
        bounds = [raw.get('min'), raw.get('max')]
        if any(bound is not None and not isinstance(bound, str) for bound in bounds):
            msg = (
                f'range filter {raw.get("field")!r}: quote min and max, '
                'as in min = "2024-01-01"'
            )
            raise JobError(msg)
        if not any((bound or '').strip() for bound in bounds):
            msg = f'range filter {raw.get("field")!r} needs a min, a max, or both'
            raise JobError(msg)
    try:
        return ValuesFilter(**raw) if 'values' in raw else RangeFilter(**raw)
    except TypeError as e:
        msg = str(e)
        raise JobError(msg) from e


def parse_job(raw: object) -> Job:
    if not isinstance(raw, dict):
        msg = f'job {raw!r} is not a table'
        raise JobError(msg)
    raw = dict(raw)
    name = raw.get('name', '?')
    if not raw.get('sheets'):
        msg = f'job {name!r}: needs at least one sheet'
        raise JobError(msg)
    site = raw.get('site')
    if not isinstance(site, str) or not site.strip():
        msg = f'job {name!r}: needs a site'
        raise JobError(msg)
    raw['site'] = site.strip()
    try:
        filters = [_parse_filter(item) for item in raw.pop('filters', [])]
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
        {k: v for k, v in item.items() if v or isinstance(v, list)}
        for item in fields['filters']
    ]
    lines = [
        '[[job]]',
        f'name = {_toml(fields.pop("name"))}',
        f'site = {_toml(fields.pop("site"))}',
    ]
    lines += [f'{key} = {_toml(value)}' for key, value in fields.items() if value]
    return '\n'.join(lines) + '\n'


def slug(text: str) -> str:
    return re.sub(r'[^\w.-]+', '_', text).strip('_') or 'export'
