# Releasing

Releases are cut by hand. There is no release CI and no semantic-release. To cut one:

1. Bump `version` in `pyproject.toml` (currently `0.1.0`).
2. Add a dated entry to `CHANGELOG.md`.
3. Update the user manual (`docs/user-manual/`) for whatever changed. Run `make docs-check`.
4. Commit the changes.
5. Tag the commit `vX.Y.Z`.
6. Push the tag.

Pushing the tag starts `.github/workflows/docs-release.yml`. It creates the GitHub release for
the tag if none exists, builds the user manual with snowball (`snowball.yaml`), and attaches
`porth-user-manual.pdf` and `.epub` to the release. The manual's version line comes from
`git describe`, so it names the tag. If the manual build fails, the release still exists and
the workflow run is red. Fix the docs, then rebuild with **Actions → docs-release → Run
workflow** and the tag as input. Use the same rebuild for a tag pushed in one `git push` with
more than three tags, because GitHub then sends no push event.

Push the tag with your own credentials. A tag pushed by another workflow using
`GITHUB_TOKEN` does not trigger `docs-release`.
