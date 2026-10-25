"""Typed keys for the state the HTTP, Kannel and admin entrypoints keep on their apps."""

import typing as tp

from aiohttp import web

from porth.adapters.queue import MessageQueue
from porth.config.settings import KannelUser, Settings
from porth.service_layer.routing import Router
from porth.service_layer.unit_of_work import AbstractUnitOfWork

# The annotated form: ty rejects Callable and Optional as AppKey's type argument
QUEUES: web.AppKey[tp.Mapping[str, MessageQueue]] = web.AppKey('queues')
ROUTER: web.AppKey[Router] = web.AppKey('router')
UOW_FACTORY: web.AppKey[tp.Callable[[], AbstractUnitOfWork]] = web.AppKey('uow_factory')
SETTINGS: web.AppKey[Settings] = web.AppKey('settings')
SMSC_STATE: web.AppKey[tp.Callable[[], dict[str, dict[str, tp.Any]]]] = web.AppKey(
    'smsc_state'
)
STARTED: web.AppKey[float] = web.AppKey('started')
MAIN_APP: web.AppKey[web.Application] = web.AppKey('main_app')
DEFAULT_SENDER: web.AppKey[tp.Optional[str]] = web.AppKey('default_sender')
USERS: web.AppKey[tp.Mapping[str, KannelUser]] = web.AppKey('users')
