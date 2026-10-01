"""Local machine metrics (psutil) and a cached connectivity check."""
from __future__ import annotations

import os
import platform
import socket
import time
from datetime import datetime
from typing import Any

import psutil

from .. import config

_net: dict[str, Any] = {"at": 0.0, "online": None}
psutil.cpu_percent(interval=None)  # prime the CPU counter so the first reading is meaningful


def lan_addresses() -> list[str]:
    out = []
    for name, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254.")):
                out.append(a.address)
    return out


def online() -> bool:
    if time.time() - _net["at"] < 60 and _net["online"] is not None:
        return _net["online"]
    ok = False
    for host in (("1.1.1.1", 53), ("8.8.8.8", 53)):
        try:
            with socket.create_connection(host, timeout=1.5):
                ok = True
                break
        except OSError:
            continue
    _net.update(at=time.time(), online=ok)
    return ok


def snapshot() -> dict[str, Any]:
    disk_root = os.path.splitdrive(str(config.BASE_DIR))[0] + "\\" if os.name == "nt" else "/"
    vm = psutil.virtual_memory()
    du = psutil.disk_usage(disk_root)
    battery = None
    try:
        b = psutil.sensors_battery()
        if b:
            battery = {"percent": round(b.percent), "plugged": b.power_plugged}
    except (AttributeError, NotImplementedError):
        pass
    return {
        "cpu_percent": round(psutil.cpu_percent(interval=None)),
        "ram_percent": round(vm.percent), "ram_used_gb": round(vm.used / 1e9, 1), "ram_total_gb": round(vm.total / 1e9, 1),
        "disk_percent": round(du.percent), "disk_free_gb": round(du.free / 1e9, 1), "disk_path": disk_root,
        "battery": battery,
        "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
        "os": f"{platform.system()} {platform.release()}",
        "hostname": platform.node(),
        "time": datetime.now().astimezone().replace(microsecond=0).isoformat(),
    }
