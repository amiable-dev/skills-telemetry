"""Whether the plugin and the package are the same release.

They are two installs and neither updates the other. The plugin ships hook
registration, the launcher, the skills and the agent; the package ships
everything that runs. Updating one and assuming the other followed is easy, and
both failure modes are quiet: a missing package leaves the launcher exiting 0 in
silence, and a stale plugin leaves stale skills with no symptom at all.

Stdlib only, and never raises: this is read at SessionStart, where a diagnostic
that throws would be worse than the drift it reports.
"""
from __future__ import annotations

import json
from pathlib import Path

PLUGIN_NAME = "stdtel"


def installed_plugin() -> tuple[str, str] | None:
    """(marketplace, version) of the installed stdtel plugin, or None.

    None is a normal state: registering the hooks with `stdtel-install settings`
    is a supported install and needs no plugin.
    """
    try:
        root = Path.home() / ".claude" / "plugins" / "marketplaces"
        for marketplace in sorted(root.iterdir()):
            manifest = marketplace / ".claude-plugin" / "plugin.json"
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("name") == PLUGIN_NAME and data.get("version"):
                return marketplace.name, str(data["version"])
    except OSError:
        pass
    return None


def skew_notice(package_version: str) -> str:
    """One line naming the drift and the command that fixes it, or "".

    Which command depends on the direction. A plugin behind the package is the
    common case today; a plugin ahead of it is the likelier one once people let
    the marketplace update itself and forget the tool.
    """
    found = installed_plugin()
    if found is None:
        return ""
    marketplace, plugin_version = found
    if plugin_version == package_version:
        return ""
    if plugin_version < package_version:
        fix = f"claude plugin update {PLUGIN_NAME}@{marketplace}"
        behind = "plugin"
    else:
        fix = "uv tool install stdtel --force --refresh"
        behind = "package"
    return (f"stdtel: plugin {plugin_version} and package {package_version} are out of step "
            f"(the {behind} is older). They are separate installs — run: {fix}")
