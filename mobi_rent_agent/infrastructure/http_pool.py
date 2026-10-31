"""Shared keep-alive HTTP sessions for VPS ↔ Farm and Farm ↔ GADS."""
from __future__ import annotations

import socket

import requests
from requests.adapters import HTTPAdapter

_POOL_CONNECTIONS = 32
_POOL_MAXSIZE = 64


class NodelayHTTPAdapter(HTTPAdapter):
    """Keep-alive pool with TCP_NODELAY so nav/control skips Nagle delay."""

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs.setdefault("socket_options", [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)])
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


def pooled_session() -> requests.Session:
    session = requests.Session()
    adapter = NodelayHTTPAdapter(
        pool_connections=_POOL_CONNECTIONS,
        pool_maxsize=_POOL_MAXSIZE,
        max_retries=0,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers["Connection"] = "keep-alive"
    return session
