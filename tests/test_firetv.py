"""Fire TV over ADB — exercised with a fake adb, no TV or network needed."""
import os
import stat
import sys

import pytest

from backend.integrations import firetv
from backend.services import settings
from backend.tools import registry
from backend.tools.registry import ToolContext, ToolError

CTX = ToolContext(actor="assistant")


class FakeADB:
    def __init__(self, connect_out="connected to 192.168.1.40:5555", state="device", packages=None):
        self.calls = []
        self.connect_out, self.state = connect_out, state
        self.packages = packages or ["com.netflix.ninja", "com.amazon.firetv.youtube", "com.spotify.tv.android"]

    def __call__(self, args, timeout=firetv.TIMEOUT):
        self.calls.append(args)
        if args[0] == "connect":
            return self.connect_out
        if args[-1] == "get-state":
            return self.state
        shell = args[3:] if args[2] == "shell" else []
        if shell[:3] == ["pm", "list", "packages"]:
            return "\n".join(f"package:{p}" for p in self.packages)
        if shell[:3] == ["cmd", "package", "resolve-activity"]:
            return "priority=0 preferredOrder=0\ncom.netflix.ninja/.MainActivity" if shell[-1] == "com.netflix.ninja" else "No activity found"
        if shell[:2] == ["am", "start"]:
            return "Starting: Intent { cmp=" + shell[-1] + " }"
        return ""


@pytest.fixture
def tv(monkeypatch):
    settings.put("firetv.host", "192.168.1.40")
    monkeypatch.setattr(firetv, "find_adb", lambda: "/fake/adb")
    fake = FakeADB()
    monkeypatch.setattr(firetv, "_run", fake)
    return fake


def test_unconfigured_tv_says_setup_required():
    assert "IP address" in firetv.setup_issue()
    with pytest.raises(ToolError) as e:
        registry.prepare("tv_remote", {"button": "pause"}, CTX)
    assert e.value.status == "unavailable"


def test_host_is_validated():
    with pytest.raises(ValueError):
        settings.put("firetv.host", "192.168.1.40; rm -rf /")
    settings.put("firetv.host", "192.168.1.40:5555")


def test_press_sends_the_right_keycode(tv):
    out = firetv.press("volume_up", 3)
    assert out["times"] == 3
    keyevents = [c for c in tv.calls if "keyevent" in c]
    assert keyevents == [["-s", "192.168.1.40:5555", "shell", "input", "keyevent", "24"]] * 3
    assert tv.calls[0] == ["connect", "192.168.1.40:5555"]


def test_unknown_button_rejected_by_schema(tv):
    with pytest.raises(ToolError) as e:
        registry.prepare("tv_remote", {"button": "self_destruct"}, CTX)
    assert e.value.status == "invalid"


def test_unauthorized_tv_gives_clear_instruction(tv):
    tv.state = "unauthorized"
    with pytest.raises(firetv.FireTVError, match="Always allow"):
        firetv.press("home")


def test_unreachable_tv(tv):
    tv.connect_out = "cannot connect to 192.168.1.40:5555: Connection refused"
    with pytest.raises(firetv.FireTVError, match="Couldn't reach"):
        firetv.press("home")


def test_open_app_by_friendly_name(tv):
    out = firetv.open_app("Netflix")
    assert sum(c[0] == "connect" for c in tv.calls) == 1
    assert out == {"opened": "com.netflix.ninja", "activity": "com.netflix.ninja/.MainActivity"}
    assert ["-s", "192.168.1.40:5555", "shell", "am", "start", "-n", "com.netflix.ninja/.MainActivity"] in tv.calls


def test_open_app_unknown_or_ambiguous(tv):
    with pytest.raises(firetv.FireTVError, match="No installed app"):
        firetv.open_app("hulu")
    tv.packages += ["com.spotify.music"]
    assert firetv.resolve_app("spotify") == "com.spotify.tv.android"  # known alias wins
    assert sum(c[0] == "connect" for c in tv.calls) == 1  # resolve reuses the connection
    tv.packages += ["com.acme.player", "org.other.player"]
    with pytest.raises(firetv.FireTVError, match="More than one"):
        firetv.open_app("player")
    with pytest.raises(firetv.FireTVError):
        firetv.open_app("com.evil;reboot")


def test_tv_needs_approval_until_you_allow_it(tv, authed):
    assert registry.prepare("tv_remote", {"button": "pause"}, CTX).needs_approval is True
    r = authed.patch("/api/tools/tv_remote", json={"auto_approve": True})
    assert r.status_code == 200 and r.json()["requires_approval"] is False
    assert registry.prepare("tv_remote", {"button": "pause"}, CTX).needs_approval is False
    # destructive tools and always-confirm tools can never skip approval
    assert authed.patch("/api/tools/delete_task", json={"auto_approve": True}).status_code == 400
    assert authed.patch("/api/tools/save_memory", json={"auto_approve": True}).status_code == 400


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell script as a fake adb")
def test_real_subprocess_path_with_fake_adb(tmp_path, monkeypatch):
    log = tmp_path / "adb.log"
    script = tmp_path / "adb"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        'case "$1" in connect) echo "connected to $2";; -s) [ "$3" = get-state ] && echo device;; esac\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    settings.put("firetv.host", "10.0.0.7")
    settings.put("firetv.adb_path", str(script))
    monkeypatch.setattr(firetv, "_server_started", False)
    assert firetv.find_adb() == str(script)
    firetv.press("play_pause")
    lines = log.read_text().splitlines()
    assert lines[0] == "start-server"
    assert "-s 10.0.0.7:5555 shell input keyevent 85" in lines
    assert os.path.exists(script)
