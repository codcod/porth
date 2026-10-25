"""Read-only HTML status page at /admin, from memory only (design.md §4.1)."""

import importlib.metadata
import time
import typing as tp

import jinja2
from aiohttp import web, web_request, web_response

from porth import metrics
from porth.entrypoints import app_keys

# templates/admin.html is the approved mockup (development/porth/mockups/admin-status.html)
# with its sample values replaced by template fields. Loaded at import, so a missing file
# fails at startup.
_ENV = jinja2.Environment(
    loader=jinja2.PackageLoader('porth.entrypoints', 'templates'),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)
_PAGE = _ENV.get_template('admin.html')


def _read(metric: tp.Any, suffix: str, *labels: str) -> dict[tuple[str, ...], float]:
    """A collector's `suffix` samples by label values; one never observed has none."""
    return {
        tuple(s.labels[n] for n in labels): s.value
        for m in metric.collect()
        for s in m.samples
        if s.name.endswith(suffix)
    }


def _uptime(seconds: int) -> str:
    days, rest = divmod(seconds, 86400)
    return f'{days} d {rest // 3600} h {rest % 3600 // 60} m'


def _n(value: float) -> str:
    return f'{int(value):,}'


async def admin_page(request: web_request.Request) -> web_response.Response:
    """Version, uptime, per-SMSC state and since-start counters, as HTML."""
    state: dict[str, dict[str, tp.Any]] = request.app[app_keys.SMSC_STATE]()
    uptime = _uptime(int(time.monotonic() - request.app[app_keys.STARTED]))
    submitted = _read(metrics.submitted, '_total', 'protocol')
    final = _read(metrics.final, '_total', 'smsc', 'status')
    retries = _read(metrics.retries, '_total', 'smsc')
    errors = _read(metrics.smpp_errors, '_total', 'smsc', 'command_status')
    receipts = _read(metrics.receipts, '_total', 'smsc', 'matched')
    mo = _read(metrics.mo, '_total', 'smsc', 'result')
    submit_sum = _read(metrics.submit_seconds, '_sum', 'smsc')
    submit_count = _read(metrics.submit_seconds, '_count', 'smsc')

    unbound = ', '.join(name for name, s in state.items() if not s['bound'])
    bound = sum(1 for s in state.values() if s['bound'])

    by_status = {s: 0.0 for s in ('delivered', 'failed', 'expired')}
    for (_, status), value in final.items():
        by_status[status] = by_status.get(status, 0.0) + value
    cards = [
        {
            'label': 'Submitted',
            'value': _n(sum(submitted.values())),
            'sub': ' · '.join(f'{p} {_n(v)}' for (p,), v in sorted(submitted.items()))
            or 'none since start',
        },
        {
            'label': 'Waiting',
            'value': f'{sum(s["waiting"] for s in state.values()):,}',
            'sub': 'queued to send, now',
        },
        {
            'label': 'Retrying',
            'value': f'{sum(s["retrying"] for s in state.values()):,}',
            'sub': 'waiting out a retry, now',
        },
        {
            'label': 'SMSCs bound',
            'value': f'{bound} of {len(state)}',
            'sub': f'{unbound} down' if unbound else 'all bound',
        },
        {
            'label': 'Final statuses',
            'value': _n(sum(final.values())),
            'sub': ' · '.join(f'{s} {_n(v)}' for s, v in by_status.items()),
        },
    ]

    def mean_submit(name: str) -> str:
        count = submit_count.get((name,), 0)
        return f'{round(submit_sum[(name,)] / count * 1000):,} ms' if count else '—'

    smscs = [
        {
            'name': name,
            'bound': s['bound'],
            'waiting': _n(s['waiting']),
            'retrying': _n(s['retrying']),
            'delivered': _n(final.get((name, 'delivered'), 0)),
            'failed': _n(final.get((name, 'failed'), 0)),
            'expired': _n(final.get((name, 'expired'), 0)),
            'retries': _n(retries.get((name,), 0)),
            'mean_submit': mean_submit(name),
        }
        for name, s in state.items()
    ]
    page = _PAGE.render(
        version=importlib.metadata.version('porth'),
        uptime=uptime,
        unbound=unbound,
        cards=cards,
        smscs=smscs,
        stale=any(smsc not in state for smsc, _ in final),
        errors=[
            {'smsc': smsc, 'command_status': cs, 'count': _n(v)}
            for (smsc, cs), v in sorted(errors.items(), key=lambda kv: -kv[1])
            if v
        ],
        receipts=[
            {
                'name': name,
                'matched': _n(receipts.get((name, 'true'), 0)),
                'unmatched': _n(receipts.get((name, 'false'), 0)),
                'mo_forwarded': _n(mo.get((name, 'forwarded'), 0)),
                'mo_failed': _n(mo.get((name, 'failed'), 0)),
            }
            for name in state
        ],
    )
    return web.Response(text=page, content_type='text/html')
