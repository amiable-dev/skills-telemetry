"""Identity that survives being in a different process.

Every hook is a separate process (ADR-003), so anything the OpenTelemetry SDK
generates per process becomes a new Prometheus series per hook invocation. A
counter that receives one increment per series can never have a rate: #42, where
`max(traces_span_metrics_calls_total)` was 1 across 563 samples and three panels
read zero forever while looking merely quiet.
"""
from __future__ import annotations

import getpass
import os
import platform
import re

from stdtel.enrich import resource_attributes
from stdtel.exporter import build_provider
from stdtel.identity import developer_hash, machine_id


def test_ids_are_stable_across_calls():
    assert machine_id() == machine_id()
    assert developer_hash() == developer_hash()


def test_ids_survive_a_stripped_environment():
    """A hook gets a non-login `sh -c` and may inherit almost nothing.

    If the identity fell back to a random value when USER or HOME was missing, it
    would be exactly as broken as the SDK's generated one, and only under the
    conditions that actually apply.
    """
    before = (machine_id(), developer_hash())
    saved = dict(os.environ)
    try:
        os.environ.clear()
        assert (machine_id(), developer_hash()) == before
    finally:
        os.environ.update(saved)


def test_ids_do_not_carry_the_username_or_hostname_in_clear():
    """The collector pseudonymises user.email; a value we emit ourselves has to
    arrive already pseudonymous, or we have moved the leak rather than closed it."""
    blob = f"{machine_id()} {developer_hash()}"
    for secret in (getpass.getuser(), platform.node(), platform.node().split(".")[0]):
        if secret:
            assert secret.lower() not in blob.lower()
    assert re.fullmatch(r"[0-9a-f]{16}", machine_id())
    assert re.fullmatch(r"[0-9a-f]{16}", developer_hash())


def test_the_machine_and_the_developer_are_different_values():
    """They answer different questions — how many series, and how many people."""
    assert machine_id() != developer_hash()


def test_the_provider_pins_service_instance_id():
    """Unset, the SDK generates a UUID per process, which prometheusremotewrite
    maps to the `instance` label — one new series per hook, forever."""
    provider = build_provider({"std.harness": "claude-code"})
    attrs = provider.resource.attributes
    assert attrs["service.instance.id"] == machine_id()


def test_two_processes_agree_on_service_instance_id():
    """The property that actually matters: the series must be the same one.

    Two providers built independently stand in for two hook processes; if these
    ever disagree the counter goes back to being stuck at 1.
    """
    a = build_provider({})
    b = build_provider({"std.team": "other"})
    assert a.resource.attributes["service.instance.id"] == b.resource.attributes["service.instance.id"]


def test_an_explicit_instance_id_still_wins():
    """Enterprise deployments may want to set their own; ours is a default."""
    provider = build_provider({"service.instance.id": "fleet-07"})
    assert provider.resource.attributes["service.instance.id"] == "fleet-07"


def test_resource_attributes_carry_the_developer_hash():
    """`distinct_users` read 0 for every skill at every volume (#43): the
    collector derived std.user.hash from user.email, which nothing ever set."""
    attrs = resource_attributes()
    assert attrs["std.user.hash"] == developer_hash()
