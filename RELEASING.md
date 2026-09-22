# Releasing

No release automation yet (no release CI workflow, no tags cut so far). To cut a release:

1. Bump `version` in `pyproject.toml` (currently `0.1.0`).
2. Add a dated entry to `CHANGELOG.md`.
3. Commit the changes.
4. Tag the commit `vX.Y.Z`.
5. Push the tag.
