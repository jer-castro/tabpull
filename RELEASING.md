# Releasing

## Set up the publisher once

`tabpull` is not on PyPI yet. Add a pending trusted publisher. Do not create an API token.

On PyPI, enter these fields.

- PyPI project name is `tabpull`.
- Owner is `jer-castro`.
- Repository name is `tabpull`.
- Workflow name is `release.yml`.
- Environment name is `pypi`.

The workflow file is `.github/workflows/release.yml`. PyPI matches the filename `release.yml`.

Create a GitHub environment named `pypi`. The publish job uses that name. PyPI matches it on upload.

TestPyPI is optional. Add a pending trusted publisher there with the same owner, repository, and workflow name. Set the environment name to `testpypi`. Create a GitHub environment named `testpypi`.

## Publish a release

1. Bump `version` in `pyproject.toml`.
2. Add a matching entry to `CHANGELOG.md`.
3. Merge that commit to `main`.
4. Tag that commit `vX.Y.Z`. The tag is the version with a leading `v`.
5. Publish a GitHub release for that tag.

Publishing the release runs the Release workflow. The workflow builds the sdist and the wheel, runs `twine check --strict`, and uploads them to PyPI. A tag push alone does not publish.

The tag must match `version`. A release tagged `v0.2.0` fails while `pyproject.toml` still says `0.1.0`.

## Try the same commit on TestPyPI

Run the Release workflow manually on the commit you want to try. That run uses the `testpypi` environment and uploads to `https://test.pypi.org/legacy/`. It does not upload to PyPI. Publish the GitHub release only after that try looks right.
