import json
import logging
import sys

from porth.logs import JsonFormatter, masked

logger = logging.getLogger('porth.test')


def record(msg='hi %s', args=('there',), exc_info=None, **extra):
    r = logger.makeRecord(
        'porth.test', logging.INFO, 'f.py', 1, msg, args, exc_info, extra=extra
    )
    return JsonFormatter().format(r)


def test_extras_become_keys():
    line = record(message_id='m1', smsc='a')
    assert '\n' not in line
    out = json.loads(line)
    assert list(out) == ['time', 'level', 'logger', 'message', 'message_id', 'smsc']
    assert out['time'].endswith('Z') and len(out['time']) == 24
    assert (out['level'], out['logger'], out['message']) == (
        'INFO',
        'porth.test',
        'hi there',
    )
    assert (out['message_id'], out['smsc']) == ('m1', 'a')


def test_traceback_goes_in_exc():
    try:
        raise ValueError('boom')
    except ValueError:
        out = json.loads(record(exc_info=sys.exc_info()))
    assert 'ValueError: boom' in out['exc']
    assert out['message'] == 'hi there'


def test_unserialisable_extra_is_str():
    class Thing:
        def __str__(self):
            return 'a thing'

    assert json.loads(record(thing=Thing()))['thing'] == 'a thing'


def test_masked_keeps_the_last_three_digits():
    assert masked('+447700900123') == '***123'
