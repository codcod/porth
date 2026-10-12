"""What the HTTP and Kannel entrypoints share."""

import ipaddress
import typing as tp


def ip_allowed(remote: tp.Optional[str], allow_ip: tp.Sequence[str]) -> bool:
    """Whether the peer address `remote` is in `allow_ip`; an empty list allows any."""
    if not allow_ip:
        return True
    if remote is None:  # not a TCP peer: no address to allow
        return False
    try:
        addr = ipaddress.ip_address(remote)
    except ValueError:
        return False
    # ponytail: the networks are parsed per request; pre-parse them if users grow long lists
    return any(addr in ipaddress.ip_network(n) for n in allow_ip)
