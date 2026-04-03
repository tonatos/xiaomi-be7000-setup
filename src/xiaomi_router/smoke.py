from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from xiaomi_router.ssh_util import RouterSSH


@dataclass
class SmokeResult:
    ok: bool
    messages: list[str]


def _check_tcp(ssh: RouterSSH, host: str, port: int) -> tuple[bool, str]:
    script = (
        f"if command -v nc >/dev/null 2>&1; then "
        f"nc -z -w 2 '{host}' {port} && echo OK || echo FAIL; "
        f"elif echo | timeout 2 cat < /dev/null > /dev/tcp/{host}/{port} 2>/dev/null; "
        f"then echo OK; else echo FAIL; fi"
    )
    _, out, _ = ssh.exec(script)
    line = out.strip().splitlines()[-1] if out.strip() else "FAIL"
    return ("OK" in line), f"tcp {host}:{port} -> {line}"


def run_smoke(ssh: RouterSSH, cfg: dict[str, Any]) -> SmokeResult:
    msgs: list[str] = []
    ok_all = True

    services = cfg.get("services", {})

    _, out, _ = ssh.exec(
        "D=$(ls /mnt/usb*/mi_docker/docker-binaries/docker 2>/dev/null | head -1); "
        "if [ -x \"$D\" ]; then \"$D\" compose version 2>&1; else echo NO_COMPOSE; fi"
    )
    msgs.append(f"compose: {out.strip()[:200]}")
    if "NO_COMPOSE" in out or "error" in out.lower():
        ok_all = False

    _, out2, _ = ssh.exec(
        "D=$(ls /mnt/usb*/mi_docker/docker-binaries/docker 2>/dev/null | head -1); "
        "[ -x \"$D\" ] && \"$D\" ps --format '{{.Names}} {{.Status}}' 2>&1 | head -20"
    )
    msgs.append(f"docker ps:\n{out2.strip()}")

    sx = services.get("xray_server", {})
    if sx.get("enabled", True):
        port = int(cfg.get("xray", {}).get("inbound", {}).get("port", 443))
        ok, m = _check_tcp(ssh, "127.0.0.1", port)
        msgs.append(m)
        ok_all = ok_all and ok

    mh = services.get("mihomo", {})
    if mh.get("enabled", True):
        for p in (mh.get("socks_port", 7890), mh.get("redir_port", 7891)):
            ok, m = _check_tcp(ssh, "127.0.0.1", int(p))
            msgs.append(m)
            ok_all = ok_all and ok

    ts = services.get("torrserver", {})
    if ts.get("enabled", True):
        p = int(ts.get("port", 8090))
        ok, m = _check_tcp(ssh, "127.0.0.1", p)
        msgs.append(m)
        ok_all = ok_all and ok

    return SmokeResult(ok=ok_all, messages=msgs)
