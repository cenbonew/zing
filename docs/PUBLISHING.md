# Publishing zing-audit to PyPI

Releases are automated by [`.github/workflows/release.yml`](../.github/workflows/release.yml),
which builds the wheel + sdist and publishes them to PyPI via **Trusted Publishing
(OIDC)** — no API token is stored as a repository secret.

## One-time setup (PyPI side)

Before the first release, register a *pending publisher* so PyPI trusts this repo:

1. Sign in to PyPI and open **Account settings → Publishing → Add a pending publisher**
   (<https://pypi.org/manage/account/publishing/>).
2. Fill in:
   - **PyPI project name:** `zing-audit`
   - **Owner:** `cenbonew`
   - **Repository name:** `zing`
   - **Workflow name:** `release.yml`
   - **Environment name:** `pypi`
3. (Recommended) In the GitHub repo, create an **Environment** named `pypi`
   (Settings → Environments) and optionally add required reviewers so a human
   approves each publish.

After the first successful publish, `zing-audit` becomes a normal Trusted Publisher
and the pending entry is consumed.

## Cutting a release

1. Update `[Unreleased]` → the new version in [`CHANGELOG.md`](../CHANGELOG.md) and
   bump `version` in [`pyproject.toml`](../pyproject.toml).
2. Commit, then tag and push:
   ```bash
   git tag -a vX.Y.Z -m "zing vX.Y.Z"
   git push origin vX.Y.Z
   ```
3. The `Release to PyPI` workflow builds, runs `twine check`, and publishes to PyPI.
4. Create the GitHub Release (notes from the CHANGELOG) if you want a release page.

## Manual fallback

If you ever need to publish by hand:

```bash
python -m pip install --upgrade build twine
python -m build
twine check dist/*
twine upload dist/*          # needs a PyPI API token
```
