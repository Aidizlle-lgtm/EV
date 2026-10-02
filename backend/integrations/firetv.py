"""Fire TV control over ADB on your home network. Nothing here touches the internet.

Setup (once): on the Fire TV turn on Developer options → ADB debugging, put Google's
Android platform-tools next to E.V. (or on PATH), enter the TV's IP in Settings, and
accept the "Allow USB debugging?" prompt that appears on the TV the first time.

Every command is a fixed argument list (no shell on this laptop). Values that reach
the TV's shell are either from a fixed key map or a package name that is validated
and confirmed installed first.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from .. import config
from ..services import settings, status

ADB_PORT = 5555
TIMEOUT = 12
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}(:\d{1,5})?$")
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.]+/[A-Za-z0-9_.$]+$")

# Android key codes (developer.android.com KeyEvent constants)
KEYS = {
    "home": 3, "back": 4, "up": 19, "down": 20, "left": 21, "right": 22, "select": 23,
    "volume_up": 24, "volume_down": 25, "menu": 82, "play_pause": 85, "next": 87, "previous": 88,
    "rewind": 89, "fast_forward": 90, "play": 126, "pause": 127, "mute": 164, "sleep": 223, "wake": 224,
}
# Friendly names → likely package names. Always confirmed against what's installed.
ALIASES = {
    "netflix": ["com.netflix.ninja"],
    "youtube": ["com.amazon.firetv.youtube", "com.google.android.youtube.tv"],
    "prime video": ["com.amazon.avod", "com.amazon.avod.thirdpartyclient"],
    "disney+": ["com.disney.disneyplus"], "disney plus": ["com.disney.disneyplus"],
    "spotify": ["com.spotify.tv.android"],
    "plex": ["com.plexapp.android"],
    "kodi": ["org.xbmc.kodi"],
}

_server_started = False


class FireTVError(Exception):
    pass


def host() -> str:
    return (settings.get("firetv.host") or "").strip()


def target() -> str:
    h = host()
    return h if ":" in h else f"{h}:{ADB_PORT}"


def find_adb() -> str | None:
    custom = (settings.get("firetv.adb_path") or "").strip().strip('"')
    if custom:
        return custom if Path(custom).is_file() else None
    exe = "adb.exe" if os.name == "nt" else "adb"
    candidates = [config.BASE_DIR / "platform-tools" / exe]
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk" / "platform-tools" / exe)
    for c in candidates:
        if c.is_file():
            return str(c)
    return shutil.which("adb")


def setup_issue() -> str | None:
    if not host():
        return "Fire TV isn't set up. Enter its IP address in Settings → Fire TV."
    if not find_adb():
        return "ADB (Android platform-tools) wasn't found. See README → Fire TV."
    return None


def configured() -> bool:
    return setup_issue() is None


def _flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _run(args: list[str], timeout: float = TIMEOUT) -> str:
    """Run adb with a fixed argument list and return combined output."""
    global _server_started
    adb = find_adb()
    if not adb:
        raise FireTVError(setup_issue() or "ADB not found.")
    if not _server_started:
        # Start the adb daemon with no pipes attached; a daemon that inherits our
        # pipes can make later calls hang until they time out (notably on Windows).
        subprocess.run([adb, "start-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       stdin=subprocess.DEVNULL, timeout=30, creationflags=_flags())
        _server_started = True
    try:
        proc = subprocess.run([adb, *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, creationflags=_flags())
    except subprocess.TimeoutExpired as e:
        raise FireTVError("The Fire TV didn't respond in time. Is it switched on and on the same Wi-Fi?") from e
    except OSError as e:
        raise FireTVError(f"Couldn't run ADB: {e}") from e
    return (proc.stdout or "") + (proc.stderr or "")


def connect() -> None:
    issue = setup_issue()
    if issue:
        raise FireTVError(issue)
    if not HOST_RE.match(host()):
        raise FireTVError("The Fire TV address in Settings doesn't look like an IP address or hostname.")
    out = _run(["connect", target()]).lower()
    if "authenticate" in out or "unauthorized" in out:
        status.record("firetv", False, "Waiting for you to allow debugging on the TV")
        raise FireTVError("The Fire TV is asking for permission. On the TV, choose “Always allow from this computer” "
                          "and press OK, then try again.")
    if "connected" not in out or "cannot" in out or "failed" in out:
        status.record("firetv", False, f"Can't reach {target()}")
        raise FireTVError(f"Couldn't reach the Fire TV at {target()}. Check the IP, that it's on, on the same Wi-Fi, "
                          "and that ADB debugging is enabled.")
    state = _run(["-s", target(), "get-state"]).strip().lower()
    if "unauthorized" in state:
        status.record("firetv", False, "Waiting for you to allow debugging on the TV")
        raise FireTVError("The Fire TV is asking for permission. On the TV, choose “Always allow from this computer” "
                          "and press OK, then try again.")
    if "device" not in state:
        status.record("firetv", False, f"Unexpected state: {state[:80]}")
        raise FireTVError(f"The Fire TV isn't ready (state: {state[:80] or 'unknown'}).")


def shell(*args: str) -> str:
    """Run a command on the TV. Call connect() once before a batch of these."""
    return _run(["-s", target(), "shell", *args])


def press(action: str, repeat: int = 1) -> dict:
    if action not in KEYS:
        raise FireTVError(f"Unknown button '{action}'.")
    repeat = max(1, min(int(repeat), 10))
    connect()
    for _ in range(repeat):
        out = _run(["-s", target(), "shell", "input", "keyevent", str(KEYS[action])])
        if "error" in out.lower() or "exception" in out.lower():
            raise FireTVError(f"The TV rejected the {action} button: {out.strip()[:200]}")
    status.record("firetv", True, f"Sent {action}")
    return {"sent": action, "times": repeat,
            "note": "ADB confirmed the button press was delivered; it can't see what's on screen."}


def installed_packages(third_party_only: bool = False, *, ensure_connected: bool = True) -> list[str]:
    if ensure_connected:
        connect()
    out = shell("pm", "list", "packages", *(["-3"] if third_party_only else []))
    pkgs = sorted({line.split(":", 1)[1].strip() for line in out.splitlines() if line.startswith("package:")})
    return [p for p in pkgs if PACKAGE_RE.match(p)]


def resolve_app(name: str) -> str:
    """Turn 'netflix' or a package name into an installed package name."""
    wanted = name.strip()
    installed = installed_packages(ensure_connected=False)
    if PACKAGE_RE.match(wanted) and wanted in installed:
        return wanted
    key = wanted.lower()
    for pkg in ALIASES.get(key, []):
        if pkg in installed:
            return pkg
    word = re.sub(r"[^a-z0-9]", "", key)
    if len(word) >= 3:
        matches = [p for p in installed if word in p.lower().replace(".", "")]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise FireTVError(f"More than one app matches “{name}”: {', '.join(matches[:8])}. Say which one.")
    raise FireTVError(f"No installed app matches “{name}”. Ask me to list the TV's apps.")


def open_app(name: str) -> dict:
    connect()
    pkg = resolve_app(name)
    for category in ("android.intent.category.LEANBACK_LAUNCHER", "android.intent.category.LAUNCHER"):
        out = shell("cmd", "package", "resolve-activity", "--brief", "-c", category, pkg)
        component = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if COMPONENT_RE.match(component):
            result = shell("am", "start", "-n", component)
            if "error" in result.lower():
                raise FireTVError(f"The TV couldn't open {pkg}: {result.strip()[:200]}")
            status.record("firetv", True, f"Opened {pkg}")
            return {"opened": pkg, "activity": component}
    result = shell("monkey", "-p", pkg, "1")
    if "no activities found" in result.lower() or "aborted" in result.lower():
        raise FireTVError(f"{pkg} is installed but has no launchable screen.")
    status.record("firetv", True, f"Opened {pkg}")
    return {"opened": pkg}


def tv_status() -> dict:
    connect()
    model = shell("getprop", "ro.product.model").strip()
    focus = shell("dumpsys", "window", "|", "grep", "mCurrentFocus")
    app = None
    m = re.search(r"\s([A-Za-z0-9_.]+)/[A-Za-z0-9_.$]+\}", focus)
    if m and PACKAGE_RE.match(m.group(1)):
        app = m.group(1)
    power = shell("dumpsys", "power", "|", "grep", "mWakefulness=")
    awake = None
    if "Awake" in power:
        awake = True
    elif "Asleep" in power or "Dozing" in power:
        awake = False
    status.record("firetv", True, f"Connected to {model or 'Fire TV'}")
    return {"connected": True, "model": model or "unknown", "foreground_app": app, "awake": awake,
            "note": "foreground_app/awake are best-effort readings from the TV."}
