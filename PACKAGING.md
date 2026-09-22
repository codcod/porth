# Packaging

`porth` is a `hatchling`-built, `src/`-layout package (`src/porth/`), with its own `uv.lock` —
unlike `stock-exchange`'s `platform/<service>` packages, it is not a `uv` workspace member. Its
one internal dependency, `smppai`, is pulled via a `git+https://github.com/codcod/smppai.git`
source in `pyproject.toml`, not a workspace path: the `smppai` checkout this umbrella tracks at
`projects/smppai` is a separate development copy, not what `porth` actually installs.

`docker-build`/`docker-run` exist as Makefile targets but no `Dockerfile` exists yet — the
Makefile marks them "optional - for future use".
