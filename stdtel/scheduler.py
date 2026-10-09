"""`stdtel-install loader`: run `stdtel-load` on a schedule (ADR-015 decision 5).

* macOS: a launchd agent with `StartInterval`, which coalesces intervals missed
  while asleep into one run on wake.
* Linux: a systemd user timer with `Persistent=true`, which only applies to
  `OnCalendar`, so the interval is a calendar step.

Neither scheduler inherits a shell profile, so the definition carries everything:
the absolute path of the `stdtel-load` beside this installation, each STDTEL_*
setting that is set now, and a PATH reaching `gh` and `git`. It holds the DSN,
so every file is mode 0600 and nothing here prints a value. Installing twice is a
no-op; a changed definition is rewritten and reloaded.

The activation commands (`launchctl`, `systemctl`) are injected, so tests never
touch the machine's scheduler.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "dev.amiable.stdtel.loader"
UNIT = "stdtel-loader"
#: Copied into the job when set. Unset ones are left to stdtel-load's defaults.
SETTINGS = ("STDTEL_DSN", "STDTEL_TEMPO", "STDTEL_LOKI", "STDTEL_LOAD_REPO", "STDTEL_HOME")
BASE_PATH = ("/usr/local/bin", "/usr/bin", "/bin")


def _which(name: str) -> str | None:
    return shutil.which(name)


def current_platform() -> str:
    return sys.platform


def load_binary() -> str:
    """The stdtel-load beside this interpreter (the tool's own bin), else on PATH."""
    sibling = Path(sys.executable).parent / "stdtel-load"
    if sibling.is_file():
        return str(sibling)
    found = shutil.which("stdtel-load")
    if found:
        return str(Path(found).resolve())
    raise FileNotFoundError("stdtel-load not found beside this installation or on PATH")


def job_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in SETTINGS if os.environ.get(k)}
    dirs = [str(Path(p).parent) for p in (_which("gh"), _which("git")) if p]
    env["PATH"] = ":".join(dict.fromkeys([*dirs, *BASE_PATH]))
    return env


# --- the definitions -----------------------------------------------------------------------

def render_launchd(binary: str, env: dict, interval: int, log: Path) -> bytes:
    return plistlib.dumps({
        "Label": LABEL, "ProgramArguments": [binary], "StartInterval": int(interval),
        "RunAtLoad": True, "EnvironmentVariables": env,
        # stdtel-load writes its own bounded log; this catches what happens before it can
        "StandardOutPath": str(log), "StandardErrorPath": str(log),
    })


def on_calendar(seconds: int) -> str:
    """A calendar step for the interval: whole minutes under an hour, whole hours above."""
    minutes = max(1, int(seconds) // 60)
    return f"*:0/{minutes}" if minutes < 60 else f"*-*-* 0/{minutes // 60}:00:00"


def _unit_safe(name: str, value: str) -> str:
    """A value inside a double-quoted unit setting: `%` escaped (systemd specifiers),
    and a quote, backslash or line break refused, since each could end the setting."""
    if any(c in value for c in '"\\\n\r'):
        raise ValueError(f"{name} holds a character a systemd unit cannot carry safely")
    return value.replace("%", "%%")


def _quoted(key: str, value: str) -> str:
    if any(c in key for c in '"= \n%'):
        raise ValueError(f"{key} is not a variable name a systemd unit can carry")
    return f'Environment="{key}={_unit_safe(key, value)}"'


def render_systemd(binary: str, env: dict, interval: int) -> tuple[str, str]:
    service = "\n".join([
        "[Unit]", "Description=stdtel: load the warehouse (stdtel-load)", "",
        "[Service]", "Type=oneshot", *(_quoted(k, v) for k, v in env.items()),
        f'ExecStart="{_unit_safe("the stdtel-load path", binary)}"', ""])
    timer = "\n".join([
        "[Unit]", "Description=stdtel: run stdtel-load on a schedule", "",
        "[Timer]", f"OnCalendar={on_calendar(interval)}", "Persistent=true", f"Unit={UNIT}.service", "",
        "[Install]", "WantedBy=timers.target", ""])
    return service, timer


# --- install / uninstall -------------------------------------------------------------------

def paths(platform: str, home: Path) -> list[Path]:
    if platform == "darwin":
        return [home / "Library" / "LaunchAgents" / f"{LABEL}.plist"]
    if platform.startswith("linux"):
        d = home / ".config" / "systemd" / "user"
        return [d / f"{UNIT}.service", d / f"{UNIT}.timer"]
    return []


_PROBES = ("print", "is-active")


def _verb(argv: list[str]) -> str:
    return argv[1] if argv[0] == "launchctl" else argv[2]


def _runner():
    """Runs a scheduler command; a failing one (not a probe) shows its own stderr."""
    def run(argv):
        r = subprocess.run(argv, capture_output=True, text=True)
        if r.returncode and _verb(argv) not in _PROBES and r.stderr.strip():
            print(r.stderr.strip()[-500:], file=sys.stderr)
        return r.returncode
    return run


def _write_private(path: Path, data: bytes) -> None:
    """A new 0600 file renamed over the old one: the DSN is never written into a
    file someone else can read, not even briefly, and a reader sees old or new."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o600)                      # whatever the umask did
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _private(f: Path) -> bool:
    return (f.stat().st_mode & 0o777) == 0o600


def _loaded(platform: str, run) -> bool:
    if platform == "darwin":
        return run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"]) == 0
    return run(["systemctl", "--user", "is-active", f"{UNIT}.timer"]) == 0


def _activate(platform: str, files: list[Path], run) -> list[list[str]]:
    if platform == "darwin":
        domain = f"gui/{os.getuid()}"
        run(["launchctl", "bootout", f"{domain}/{LABEL}"])          # not loaded yet is fine
        return [["launchctl", "bootstrap", domain, str(files[0])]]
    return [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"]]


def install(platform: str, home: Path, binary: str, interval: int, run=None) -> int:
    run = run or _runner()
    files = paths(platform, home)
    if not files:
        print(f"stdtel-install: no scheduler support for {platform}; run stdtel-load from your own "
              "scheduler instead", file=sys.stderr)
        return 1
    env = job_env()
    if platform == "darwin":
        log = Path(env.get("STDTEL_HOME") or home / ".stdtel") / "loader.log"
        log.parent.mkdir(parents=True, exist_ok=True)   # launchd will not create it
        wanted = [render_launchd(binary, env, interval, log)]
        schedule = f"every {interval} s"
    else:
        wanted = [t.encode() for t in render_systemd(binary, env, interval)]
        schedule = f"OnCalendar={on_calendar(interval)}"
    current = all(f.exists() and f.read_bytes() == w and _private(f) for f, w in zip(files, wanted))
    if current and _loaded(platform, run):
        print(f"stdtel-install: loader already installed ({files[0]}), {schedule}")
        return 0
    if not current:
        for f, w in zip(files, wanted):
            _write_private(f, w)
    for argv in _activate(platform, files, run):
        if run(argv) != 0:
            print(f"stdtel-install: `{' '.join(argv[:3])} …` failed; the definition is written at "
                  f"{files[0]} and the next `stdtel-install loader` retries", file=sys.stderr)
            return 1
    print(f"stdtel-install: loader scheduled, {schedule} ({files[0]}, mode 0600). "
          f"Settings carried: {', '.join(k for k in env if k != 'PATH') or 'none'}; values are not shown.")
    return 0


def uninstall(platform: str, home: Path, run=None) -> int:
    run = run or _runner()
    files = [f for f in paths(platform, home) if f.exists()]
    if not files:
        print("stdtel-install: no loader schedule installed")
        return 0
    if _loaded(platform, run):
        stop = (["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"] if platform == "darwin"
                else ["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"])
        if run(stop) != 0:
            print(f"stdtel-install: could not stop the loader job; it is still scheduled, and "
                  f"{files[0]} is kept so a retry can find it", file=sys.stderr)
            return 1
    for f in files:
        f.unlink()
    if not platform == "darwin":
        run(["systemctl", "--user", "daemon-reload"])
    print(f"stdtel-install: loader schedule removed ({files[0]})")
    return 0
