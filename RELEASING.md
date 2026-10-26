# Releasing

Releases are cut by hand: there is no semantic-release. The tag push then publishes the manual
and the container image (below). To cut one:

1. Bump `version` in `pyproject.toml` (currently `0.5.0`).
2. Add a dated entry to `CHANGELOG.md`.
3. Update the user manual (`docs/user-manual/`) for whatever changed. Run `make docs-check`
   and `make docs-build`. The build renders the PDF and EPUB the same way the release workflow
   does, so it catches what the check misses (text that asciidoctor-pdf can't parse).
4. Commit the changes, push them to `main` (or merge their pull request) and wait for CI to
   go green.
5. Tag the commit `vX.Y.Z`.
6. Push the tag.

CI is `.github/workflows/ci.yml`. It runs lint, the tests, `make docs-check` and
`make docs-build` on every pull request and push to `main`, but not on tags, so step 4 is what
shows the tagged commit is green.

Pushing the tag starts `.github/workflows/docs-release.yml`. It creates the GitHub release for
the tag if none exists, builds the user manual with snowball (`snowball.yaml`), and attaches
`porth-user-manual.pdf` and `.epub` to the release. The manual's version line comes from
`git describe`, so it names the tag. If the manual build fails, the release still exists and
the workflow run is red. The rebuild, **Actions → docs-release → Run workflow** with the tag
as input, checks out the tag. It therefore only helps when the failure was outside the docs,
such as a runner or Homebrew hiccup. A fix to the docs needs a new patch release and tag
(v0.4.0's manual needed v0.4.1). Use the same rebuild for a tag pushed in one `git push` with
more than three tags, because GitHub then sends no push event.

Pushing the tag also starts `.github/workflows/image.yml`. It builds the `Dockerfile` for
`linux/amd64` and `linux/arm64` and pushes `ghcr.io/codcod/porth:X.Y.Z` and `:latest`. To
republish an existing tag, use **Actions → image → Run workflow** with the tag as input. A
republish pushes only `:X.Y.Z` and leaves `:latest` where it is. A push of more than three
tags at once starts no `image` run either, and a republish can't move `:latest`. `:latest`
follows whichever tag push ran last, so push only `vX.Y.Z` tags, one per `git push`, in
version order.

After the first image publish, make the package public once: open it on GitHub, then
**Package settings → Change visibility → Public**. A first push can create it as private,
and a workflow token can't change that.

Push the tag with your own credentials. A tag pushed by another workflow using
`GITHUB_TOKEN` triggers neither `docs-release` nor `image`.
