"""Logging setup: monobase's text lines, or one JSON object per line."""

import json
import logging
import time
import typing as tp

import monobase.config

# what every record has; anything else on one came in through `extra`
_PLAIN = {
    *logging.LogRecord('', 0, '', 0, '', None, None).__dict__,
    'message',
    'asctime',
}


class JsonFormatter(logging.Formatter):
    """time (UTC), level, logger, message, exc (with a traceback), then the extras."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(record.created))
        out: dict[str, tp.Any] = {
            'time': f'{stamp}.{int(record.msecs):03d}Z',
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }
        if record.exc_info:
            out['exc'] = self.formatException(record.exc_info)
        out.update((k, v) for k, v in record.__dict__.items() if k not in _PLAIN)
        return json.dumps(out, ensure_ascii=False, default=str)


def setup_logging(level: str, fmt: str = 'text') -> None:
    """monobase's setup at level; fmt 'json' swaps in JsonFormatter."""
    monobase.config.setup_logging(level)
    if fmt == 'json':
        for handler in logging.getLogger().handlers:
            handler.setFormatter(JsonFormatter())
