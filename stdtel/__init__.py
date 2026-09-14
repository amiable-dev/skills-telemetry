"""stdtel — standards-as-skills telemetry."""
from __future__ import annotations


def _installed_version() -> str:
    """The version of the package that is actually running.

    Derived, never written down. A hardcoded copy here read `0.1.0` from the
    first commit until 0.3.1 shipped — wrong for every release, and nothing
    noticed because nothing read it. A version that can drift silently is worse
    than no version: the plugin-skew check below now depends on this being true.
    """
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("stdtel")
    except PackageNotFoundError:          # running from a source tree
        import pathlib
        import re
        pyproject = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"
        try:
            m = re.search(r'^version = "([^"]+)"', pyproject.read_text(), re.M)
        except OSError:
            return "unknown"
        return m.group(1) if m else "unknown"


__version__ = _installed_version()
