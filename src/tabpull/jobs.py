import json
import re
import tomllib
from collections.abc import Sequence
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


def _jobs_from_text(text: str) -> list[Job]:
    raw_jobs = tomllib.loads(text).get('job', [])
    if not isinstance(raw_jobs, list):
        msg = '`job` must be an array of tables'
        raise JobError(msg)
    return [parse_job(raw) for raw in raw_jobs]


def load_jobs(path: Path) -> list[Job]:
    if not path.exists():
        return []
    return _jobs_from_text(path.read_text(encoding='utf-8'))


def save_jobs(path: Path, jobs: Sequence[Job]) -> None:
    names = [job.name for job in jobs]
    if any(not name.strip() for name in names):
        msg = 'job name is empty'
        raise JobError(msg)
    if dupes := sorted({name for name in names if names.count(name) > 1}):
        msg = f'duplicate job names: {", ".join(dupes)}'
        raise JobError(msg)
    text = '\n'.join(job_to_toml(job) for job in jobs)
    _jobs_from_text(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + '.tmp')
    scratch.write_text(text, encoding='utf-8')
    scratch.replace(path)


def _unknown(names: Sequence[str], jobs: Sequence[Job]) -> JobError:
    saved = ', '.join(job.name for job in jobs) or 'none'
    return JobError(f'Unknown jobs: {", ".join(names)}. Saved jobs: {saved}')


def update_job(path: Path, old_name: str, job: Job) -> list[Job]:
    jobs = load_jobs(path)
    index = next((i for i, saved in enumerate(jobs) if saved.name == old_name), None)
    if index is None:
        raise _unknown([old_name], jobs)
    if job.name != old_name and any(saved.name == job.name for saved in jobs):
        msg = f'A job named {job.name!r} already exists in {path}.'
        raise JobError(msg)
    jobs[index] = job
    save_jobs(path, jobs)
    return jobs


def remove_jobs(path: Path, names: Sequence[str]) -> list[Job]:
    jobs = load_jobs(path)
    if unknown := sorted(set(names) - {job.name for job in jobs}):
        raise _unknown(unknown, jobs)
    kept = [job for job in jobs if job.name not in names]
    save_jobs(path, kept)
    return kept


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
