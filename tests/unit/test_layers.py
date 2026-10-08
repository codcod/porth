"""The layers' dependency direction (design.md §4.6): domain imports nothing from
porth, adapters never import the service layer or entrypoints, the service layer never
imports entrypoints. config and metrics are not layers."""

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / 'src' / 'porth'

FORBIDDEN = {
    'domain': ('porth',),
    'adapters': ('porth.service_layer', 'porth.entrypoints'),
    'service_layer': ('porth.entrypoints',),
}


def imported(path: pathlib.Path) -> set[str]:
    """Every porth module path imports, `from porth import x` as porth.x."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # relative imports would slip past the prefix check
            assert node.level == 0 and node.module, f'{path.name}: relative import'
            names.update(f'{node.module}.{alias.name}' for alias in node.names)
    return {n for n in names if n == 'porth' or n.startswith('porth.')}


@pytest.mark.parametrize('layer', FORBIDDEN)
def test_layer_imports_no_forbidden_layer(layer):
    modules = sorted((SRC / layer).glob('*.py'))
    assert modules
    bad = [
        f'{path.name}: {name}'
        for path in modules
        for name in sorted(imported(path))
        if any(name == f or name.startswith(f + '.') for f in FORBIDDEN[layer])
    ]
    assert not bad
