"""Unit tests for the SMSC router (design.md §4.2 Routing)."""

import pytest

from porth.config.settings import RoutingConfig
from porth.domain.exceptions import NoRoute
from porth.service_layer.routing import Router

NAMES = ('uk', 'uk-mobile', 'intl')


def router(default=None, **prefixes) -> Router:
    return Router(NAMES, RoutingConfig(default=default, prefixes=prefixes))


R = router(**{'44': 'uk', '4470': 'uk-mobile', '00': 'intl'})


@pytest.mark.parametrize(
    'number, smsc',
    [
        ('447012345678', 'uk-mobile'),  # the longest match wins
        ('441234567890', 'uk'),
        ('+447012345678', 'uk-mobile'),  # matched on digits only
        ('+44-70-1234-5678', 'uk-mobile'),
        ('44 70 12345678', 'uk-mobile'),
        ('00447012345678', 'intl'),  # 00 is not rewritten
    ],
)
def test_longest_prefix_wins(number, smsc):
    assert R.route(number) == smsc


def test_default_when_no_prefix_matches():
    assert router('intl', **{'44': 'uk'}).route('15550000001') == 'intl'


def test_no_default_is_no_route():
    with pytest.raises(NoRoute, match='15550000001'):
        R.route('15550000001')


def test_known_smsc_overrides_the_prefix():
    assert R.route('447012345678', 'intl') == 'intl'


def test_unknown_smsc_is_no_route():
    with pytest.raises(NoRoute, match="'zz'"):
        router('uk').route('447012345678', 'zz')


@pytest.mark.parametrize(
    'config, key',
    [
        (RoutingConfig(prefixes={'4a': 'uk'}), 'porth.routing.prefixes."4a"'),
        (RoutingConfig(prefixes={'+44': 'uk'}), r'porth.routing.prefixes."\+44"'),
        (RoutingConfig(prefixes={'٤٤': 'uk'}), 'porth.routing.prefixes."٤٤"'),
        (RoutingConfig(prefixes={'44': 'nope'}), 'porth.routing.prefixes."44"'),
        (RoutingConfig(default='nope'), 'porth.routing.default'),
    ],
)
def test_bad_rules_raise_naming_the_key(config, key):
    with pytest.raises(ValueError, match=f'invalid {key}'):
        Router(NAMES, config)


def test_empty_smsc_name_is_rejected():
    with pytest.raises(ValueError, match='porth.smsc.""'):
        Router(('', 'uk'), RoutingConfig())
