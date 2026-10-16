"""The shipped Grafana dashboard and Prometheus rules name only metrics porth exports, so a
renamed metric fails here rather than in an operator's Grafana."""

import json
import pathlib
import re

from prometheus_client.metrics import MetricWrapperBase

from porth import metrics

DEPLOY = pathlib.Path(__file__).resolve().parents[2] / 'deploy'
FILES = [DEPLOY / 'grafana' / 'porth.json', DEPLOY / 'prometheus' / 'alerts.yml']
SUFFIXES = {
    'counter': ('_total',),
    'gauge': ('',),
    'histogram': ('_bucket', '_count', '_sum'),
}


def test_deploy_files_name_only_exported_metrics() -> None:
    json.loads(FILES[0].read_text())
    exported = {
        m._name + suffix
        for m in vars(metrics).values()
        if isinstance(m, MetricWrapperBase)
        for suffix in SUFFIXES[m._type]
    }
    for path in FILES:
        names = set(re.findall(r'porth_[a-z_]+', path.read_text()))
        assert names, f'{path.name}: no porth metric found'
        assert names <= exported, f'{path.name}: unknown {names - exported}'
