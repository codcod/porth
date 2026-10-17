"""Read-only HTML status page at /admin, from memory only (design.md §4.1)."""

import html
import importlib.metadata
import string
import time
import typing as tp

from aiohttp import web, web_request, web_response

from porth import metrics

# The approved mockup (development/porth/mockups/admin-status.html), its sample values
# replaced by $placeholders. string.Template, as the CSS is full of {}.
_PAGE = string.Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>porth admin</title>
<style>
:root{--bg:#f4f5f7;--panel:#fff;--text:#1a1d23;--muted:#5b6470;--border:#dfe3e8;--accent:#2f6fed;--pill-bg:#eef1f5;--good:#1d55c4;--good-bg:#e6eefc;--bad:#a92b16;--bad-bg:#fbe9e5}
@media (prefers-color-scheme: dark){:root{--bg:#14161a;--panel:#1c1f24;--text:#e7e9ec;--muted:#9aa2ad;--border:#2c3038;--accent:#6ea3ff;--pill-bg:#262a31;--good:#9cc0ff;--good-bg:#1f2b42;--bad:#ff8f7a;--bad-bg:#3a231f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
a{color:var(--accent)}
header{display:flex;flex-wrap:wrap;gap:.5rem 1.5rem;align-items:center;justify-content:space-between;padding:.75rem 1.5rem;background:var(--panel);border-bottom:1px solid var(--border)}
header span,.note{color:var(--muted);font-size:.85rem}
main{max-width:72rem;margin:0 auto;padding:1.5rem 1rem;display:flex;flex-direction:column;gap:1.25rem}
h1{margin:0;font-size:1.5rem}h2{margin:0 0 .5rem;font-size:1.05rem}p{margin:0}
.alert{background:var(--bad-bg);color:var(--bad);border-radius:8px;padding:.75rem 1rem;font-size:.9rem}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(11rem,1fr));gap:1rem}
.card,.panel{background:var(--panel);border:1px solid var(--border);border-radius:8px}
.card{padding:.75rem 1rem}.panel{padding:1rem 1.25rem}
.label,.sub{color:var(--muted);font-size:.8rem}.value{font-size:1.4rem;font-weight:600;font-variant-numeric:tabular-nums}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(22rem,1fr));gap:1.25rem}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:.45rem .6rem;border-bottom:1px solid var(--border);white-space:nowrap}
th{color:var(--muted);font-weight:500;font-size:.82rem}
.n{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
.pill{display:inline-block;border-radius:999px;padding:.1rem .6rem;font-size:.8rem;background:var(--pill-bg);color:var(--muted)}
.pill.good{background:var(--good-bg);color:var(--good);font-weight:600}
.pill.bad{background:var(--bad-bg);color:var(--bad);font-weight:600}
</style>
</head>
<body>
<header><strong>porth admin</strong><span>v$version · up $uptime · refreshes every 10 s</span></header>
<main>
<div><h1>Status</h1><p class="note">Counts since porth started ($uptime ago): every counter resets at a restart. Waiting and retrying are live.</p></div>
$alert<div class="cards">
$cards</div>
<section class="panel"><h2>SMSCs</h2><div class="scroll"><table>
<thead><tr><th>SMSC</th><th>State</th><th class="n">Waiting</th><th class="n">Retrying</th><th class="n">Delivered</th><th class="n">Failed</th><th class="n">Expired</th><th class="n">Retries</th><th class="n">Mean submit</th></tr></thead>
<tbody>
$smsc_rows</tbody></table></div>$stale_note</section>
<div class="two">
<section class="panel"><h2>SMPP errors</h2><div class="scroll"><table>
<thead><tr><th>SMSC</th><th>command_status</th><th class="n">Count</th></tr></thead>
<tbody>
$error_rows</tbody></table></div></section>
<section class="panel"><h2>Receipts and MO</h2><div class="scroll"><table>
<thead><tr><th>SMSC</th><th class="n">Receipts matched</th><th class="n">Unmatched</th><th class="n">MO forwarded</th><th class="n">MO failed</th></tr></thead>
<tbody>
$receipt_rows</tbody></table></div></section>
</div>
<p class="note">Machine-readable: <a href="/metrics">/metrics</a> (Prometheus) · <a href="/status">/status</a> · <a href="/ready">/ready</a>. Lifetime totals and history: the Grafana dashboard in <code>deploy/grafana/porth.json</code>.</p>
</main>
</body>
</html>
""")


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
    return f'<td class="n">{int(value):,}</td>'


def _code(name: str) -> str:
    return f'<td><code>{html.escape(name)}</code></td>'


def _card(label: str, value: str, sub: str) -> str:
    return (
        f'<div class="card"><div class="label">{label}</div>'
        f'<div class="value">{value}</div><div class="sub">{sub}</div></div>\n'
    )


def _rows(rows: list[str], empty: str, columns: int) -> str:
    if not rows:
        return f'<tr><td class="note" colspan="{columns}">{empty}</td></tr>\n'
    return ''.join(f'<tr>{r}</tr>\n' for r in rows)


async def admin_page(request: web_request.Request) -> web_response.Response:
    """Version, uptime, per-SMSC state and since-start counters, as HTML."""
    state: dict[str, dict[str, tp.Any]] = request.app['smsc_state']()
    uptime = _uptime(int(time.monotonic() - request.app['started']))
    submitted = _read(metrics.submitted, '_total', 'protocol')
    final = _read(metrics.final, '_total', 'smsc', 'status')
    retries = _read(metrics.retries, '_total', 'smsc')
    errors = _read(metrics.smpp_errors, '_total', 'smsc', 'command_status')
    receipts = _read(metrics.receipts, '_total', 'smsc', 'matched')
    mo = _read(metrics.mo, '_total', 'smsc', 'result')
    submit_sum = _read(metrics.submit_seconds, '_sum', 'smsc')
    submit_count = _read(metrics.submit_seconds, '_count', 'smsc')

    unbound = [name for name, s in state.items() if not s['bound']]
    alert = ''
    if unbound:
        names = html.escape(', '.join(unbound))
        alert = (
            f'<div class="alert" role="status"><strong>{names} unbound.</strong>'
            ' /ready answers 503 until every SMSC is bound.</div>\n'
        )

    by_status = {s: 0.0 for s in ('delivered', 'failed', 'expired')}
    for (_, status), value in final.items():
        by_status[status] = by_status.get(status, 0.0) + value
    cards = ''.join(
        [
            _card(
                'Submitted',
                f'{int(sum(submitted.values())):,}',
                ' · '.join(
                    f'{html.escape(p)} {int(v):,}'
                    for (p,), v in sorted(submitted.items())
                )
                or 'none since start',
            ),
            _card(
                'Waiting',
                f'{sum(s["waiting"] for s in state.values()):,}',
                'queued to send, now',
            ),
            _card(
                'Retrying',
                f'{sum(s["retrying"] for s in state.values()):,}',
                'waiting out a retry, now',
            ),
            _card(
                'SMSCs bound',
                f'{len(state) - len(unbound)} of {len(state)}',
                f'{html.escape(", ".join(unbound))} down' if unbound else 'all bound',
            ),
            _card(
                'Final statuses',
                f'{int(sum(final.values())):,}',
                ' · '.join(
                    f'{html.escape(s)} {int(v):,}' for s, v in by_status.items()
                ),
            ),
        ]
    )

    def mean_submit(name: str) -> str:
        count = submit_count.get((name,), 0)
        ms = f'{round(submit_sum[(name,)] / count * 1000):,} ms' if count else '—'
        return f'<td class="n">{ms}</td>'

    smsc_rows = [
        _code(name)
        + (
            '<td><span class="pill good">bound</span></td>'
            if s['bound']
            else '<td><span class="pill bad">unbound</span></td>'
        )
        + _n(s['waiting'])
        + _n(s['retrying'])
        + ''.join(
            _n(final.get((name, st), 0)) for st in ('delivered', 'failed', 'expired')
        )
        + _n(retries.get((name,), 0))
        + mean_submit(name)
        for name, s in state.items()
    ]
    stale = any(smsc not in state for smsc, _ in final)
    error_rows = [
        _code(smsc) + f'<td><span class="pill">{html.escape(cs)}</span></td>' + _n(v)
        for (smsc, cs), v in sorted(errors.items(), key=lambda kv: -kv[1])
        if v
    ]
    receipt_rows = [
        _code(name)
        + _n(receipts.get((name, 'true'), 0))
        + _n(receipts.get((name, 'false'), 0))
        + _n(mo.get((name, 'forwarded'), 0))
        + _n(mo.get((name, 'failed'), 0))
        for name in state
    ]

    page = _PAGE.substitute(
        version=html.escape(importlib.metadata.version('porth')),
        uptime=uptime,
        alert=alert,
        cards=cards,
        smsc_rows=_rows(smsc_rows, 'No SMSCs configured.', 9),
        stale_note=(
            '<p class="note">Totals above can include SMSCs no longer configured.</p>'
            if stale
            else ''
        ),
        error_rows=_rows(error_rows, 'None since start.', 3),
        receipt_rows=_rows(receipt_rows, 'No SMSCs configured.', 5),
    )
    return web.Response(text=page, content_type='text/html')
