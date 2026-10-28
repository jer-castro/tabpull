# Releasing

release-please reads Conventional Commits on `main` and opens one release pull request. You merge that pull request. The same workflow tags `vX.Y.Z`, builds that tag, and uploads to PyPI.

GitHub does not start a workflow from a release created with `GITHUB_TOKEN`. Release-please and the publish job therefore run in `.github/workflows/release.yml`. The publish job runs only when this run's `release_created` output is true. A personal access token is not used. A release you publish by hand does not upload.

## Set the publisher up once

Keep the PyPI trusted publisher as it is.

- Project name is `tabpull`.
- Owner is `jer-castro`.
- Repository is `tabpull`.
- Workflow filename is `release.yml`.
- Environment name is `pypi`.

Do not rename the workflow file. PyPI matches the filename `release.yml`.

The publish job uses the GitHub environment `pypi`. The trusted publisher is registered to that environment name. Keep the name `pypi`. The `pypi` environment allows deployments from branch `main` and from tags matching `v*`.

For TestPyPI, add a trusted publisher with the same owner, repository, and workflow filename. Set the environment name to `testpypi`. The manual run uses that environment.

Allow GitHub Actions to create and approve pull requests. The setting is under Settings, Actions, General. release-please cannot open its pull request without it.

## Write commits release-please reads

Use a Conventional Commit as the squash title.

- `feat` opens a minor release.
- `fix`, `perf`, `deps`, `revert`, and `docs` open a patch release.
- A breaking change before 1.0.0 opens the next minor release. Put `Release-As: 1.0.0` in the commit body when you want 1.0.0.
- `build`, `ci`, `chore`, `refactor`, `style`, and `test` do not open a release on their own.

Squash-merge, and keep that squash title conventional.

## Merge the release pull request

release-please keeps one pull request open. The title looks like `chore(main): release 0.2.0`.

That pull request updates `version` in `pyproject.toml`, the `tabpull` version line in `uv.lock`, `CHANGELOG.md`, and `.release-please-manifest.json`. The existing `## 0.1.0` entry stays under the new section.

`version` in `pyproject.toml` is the only version source. release-please copies it into `uv.lock`. `tabpull --version` reads installed metadata, which the build fills from `pyproject.toml`.

Merge the pull request. Do not edit the version or the changelog by hand.

GitHub does not run checks on a pull request opened with `GITHUB_TOKEN`. To build that branch first, run the TestPyPI command below with ref `release-please--branches--main`. CI runs on `main` after the merge.

## Publish to PyPI

The merge runs Release on `main`. release-please creates tag `vX.Y.Z` and the GitHub Release in that run. The workflow builds that tag and uploads the sdist and the wheel to PyPI.

If the upload fails, re-run the failed jobs on that same run. Do not start a manual run to retry PyPI. A manual run uploads only to TestPyPI.

## Try a commit on TestPyPI

The TestPyPI trusted publisher has to exist before this uploads. This repository change does not perform that upload.

```bash
gh workflow run release.yml --ref <branch>
```

The run uses the `testpypi` environment and `https://test.pypi.org/legacy/`. It does not request `pypi`. The `testpypi` environment has no branch limit, so a dry run from a pull request branch is allowed.

The workflow appends `.dev` and the GitHub run number to the version, and only on this manual run. Each new run uploads a new version, so a version already on TestPyPI does not collide. A re-run of the same workflow run reuses the number and can collide. Start a new run instead.

```bash
pip install --pre --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ 'tabpull==X.Y.Z.devN'
```
