"""Stable, pseudonymous identity for this machine and this developer.

Every hook runs in its own process (ADR-003), so any identity the OpenTelemetry
SDK *generates* is fresh each time. `service.instance.id` is the one that bites:
the prometheusremotewrite exporter maps it to the `instance` label, so a
generated one puts every span in a brand-new time series which receives exactly
one increment and is then abandoned. `rate()` needs two points in the same
series; it got one, so three panels of the operational dashboard read zero at
any volume while looking merely quiet (#42). Cardinality grew by one series set
per hook invocation, without bound.

So identity is *derived*, never generated, and derived from values that exist
even under `env -i` — a hook gets a non-login `sh -c` and may inherit nothing.

Both values are SHA-256 truncated to 16 hex characters: neither the username nor
the hostname appears in clear, and neither is reversible without guessing the
input. That is the same property the collector's pseudonymise processor provides
for `user.email`, applied before the value ever leaves the process.

Module scope stays stdlib-only, like the rest of the hook path.
"""
from __future__ import annotations

import hashlib
import os
import platform

_ID_LENGTH = 16


def _digest(namespace: str, raw: str) -> str:
    return hashlib.sha256(f"{namespace}:{raw}".encode()).hexdigest()[:_ID_LENGTH]


def _host() -> str:
    """Hostname without the environment.

    platform.node() reads uname(2), so it works with no environment at all.
    """
    try:
        return platform.node() or "unknown-host"
    except Exception:                             # noqa: BLE001 - identity must never raise
        return "unknown-host"


def _user() -> str:
    """The OS user, as an id rather than a name.

    getpass.getuser() consults LOGNAME/USER/LNAME/USERNAME first and only then
    falls back to the password database, so under a stripped environment it can
    raise. The uid needs no environment and is stable across a rename.
    """
    try:
        return str(os.getuid())
    except AttributeError:                        # Windows has no getuid
        return os.environ.get("USERNAME") or "unknown-user"


def machine_id() -> str:
    """Value for `service.instance.id`: one series per machine, not per process."""
    return _digest("stdtel-machine", _host())


def developer_hash() -> str:
    """Value for `std.user.hash`: how many *people* used a skill, not how many runs.

    User and host together, so one person on two machines counts twice and two
    people sharing a machine count separately. The first is a known overcount and
    is documented where the number is read; it is the honest direction to err,
    because the floor it feeds ("5+ developers") is a minimum.
    """
    return _digest("stdtel-user", f"{_user()}@{_host()}")
