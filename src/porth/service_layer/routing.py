"""Which SMSC a message goes out through (design.md §4.2 Routing)."""

import re
import typing as tp

from porth.config.settings import RoutingConfig
from porth.domain.exceptions import NoRoute


class Router:
    """A requested SMSC, else the longest matching destination prefix, else the default."""

    def __init__(self, names: tp.Collection[str], config: RoutingConfig):
        if '' in names:
            raise ValueError('invalid porth.smsc."": an SMSC needs a name')
        for prefix, name in config.prefixes.items():
            key = f'porth.routing.prefixes."{prefix}"'
            if not re.fullmatch('[0-9]+', prefix):
                raise ValueError(f'invalid {key}: a prefix is digits only')
            if name not in names:
                raise ValueError(f'invalid {key}: no SMSC named {name!r}')
        if config.default is not None and config.default not in names:
            raise ValueError(
                f'invalid porth.routing.default: no SMSC named {config.default!r}'
            )
        self.names = names
        self.config = config

    def route(self, number: str, smsc: tp.Optional[str] = None) -> str:
        """The SMSC's name for number; NoRoute if there is none."""
        if smsc is not None:
            if smsc not in self.names:
                raise NoRoute(f'no SMSC named {smsc!r}')
            return smsc
        # Digits only (+, -, spaces dropped); 00 is not rewritten (design.md 1.37)
        digits = re.sub('[^0-9]', '', number)
        for i in range(len(digits), 0, -1):
            if name := self.config.prefixes.get(digits[:i]):
                return name
        if self.config.default is None:
            raise NoRoute(f'no route for {number!r}')
        return self.config.default
