from __future__ import annotations

from typing import Any

from xiaomi_router.config_loader import require_router_password
from xiaomi_router.ssh_util import RouterSSH

COMMANDS = [
    "cat /etc/init.d/mi_docker 2>/dev/null | head -80",
    "cat /etc/config/mi_docker 2>/dev/null",
    "ls -la /mnt/usb*/ 2>/dev/null | head -40",
    "ls -la /mnt/usb*/mi_docker/docker-binaries/ 2>/dev/null | head -20",
    "ps | grep -iE 'docker|containerd' | grep -v grep",
    "/etc/init.d/mi_docker status 2>&1; echo EXIT=$?",
    "logread | grep -i docker | tail -25",
    "mount | grep cgroup | head -10",
    "DEVICE_UUID=$(uci -q get mi_docker.settings.device_uuid); "
    'STORAGE_DIR=$(storage dump | grep -C3 "${DEVICE_UUID:-x}" | grep target: | awk \'{print $2}\'); '
    'echo "STORAGE_DIR=$STORAGE_DIR"',
]


def run_diagnose(cfg: dict[str, Any]) -> None:
    router = cfg["router"]
    ssh = RouterSSH(
        host=str(router["host"]),
        password=require_router_password(cfg),
        port=int(router.get("ssh_port", 22)),
        username=str(router.get("ssh_user", "root")),
    )
    try:
        for cmd in COMMANDS:
            print(f"\n{'=' * 60}\n>>> {cmd}\n{'=' * 60}")
            print(ssh.exec_text(cmd))
    finally:
        ssh.close()
