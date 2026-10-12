# Releasing

Releases are cut by hand. There is no release CI and no semantic-release. To cut one:

1. Bump `version` in `pyproject.toml` (currently `0.2.1`).
2. Add a dated entry to `CHANGELOG.md`.
3. Update the user manual (`docs/user-manual/`) for whatever changed. Run `make docs-check`.
4. Commit the changes, push them to `main` (or merge their pull request) and wait for CI to
   go green.
5. Tag the commit `vX.Y.Z`.
6. Push the tag.

CI is `.github/workflows/ci.yml`. It runs lint, the tests and `make docs-check` on every pull
request and push to `main`, but not on tags, so step 4 is what shows the tagged commit is green.

Pushing the tag starts `.github/workflows/docs-release.yml`. It creates the GitHub release for
the tag if none exists, builds the user manual with snowball (`snowball.yaml`), and attaches
`porth-user-manual.pdf` and `.epub` to the release. The manual's version line comes from
`git describe`, so it names the tag. If the manual build fails, the release still exists and
the workflow run is red. Fix the docs, then rebuild with **Actions → docs-release → Run
workflow** and the tag as input. Use the same rebuild for a tag pushed in one `git push` with
more than three tags, because GitHub then sends no push event.

Push the tag with your own credentials. A tag pushed by another workflow using
`GITHUB_TOKEN` does not trigger `docs-release`.
