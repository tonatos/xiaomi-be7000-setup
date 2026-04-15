from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from xiaomi_router.ssh_util import RouterSSH


@dataclass
class SmokeResult:
    ok: bool
    messages: list[str]


def _check_tcp(ssh: RouterSSH, host: str, port: int) -> tuple[bool, str]:
    script = (
        f"netstat -tln 2>/dev/null | grep -Eq '[:.]({port})\\b' "
        "&& echo OK || echo FAIL"
    )
    _, out, _ = ssh.exec(script)
    line = out.strip().splitlines()[-1] if out.strip() else "FAIL"
    return ("OK" in line), f"tcp {host}:{port} -> {line}"


def _wait_tcp(
    ssh: RouterSSH,
    host: str,
    port: int,
    timeout_s: int = 30,
    interval_s: int = 3,
    log: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    if log is not None:
        log(f"проверка tcp {host}:{port} (таймаут {timeout_s}s)")
    deadline = time.time() + timeout_s
    last_msg = "FAIL"
    started = time.time()
    while time.time() < deadline:
        ok, msg = _check_tcp(ssh, host, port)
        last_msg = msg
        if ok:
            return True, f"{msg} (ready)"
        if log is not None:
            elapsed = int(time.time() - started)
            log(f"ожидание tcp {host}:{port}... {elapsed}/{timeout_s}s")
        time.sleep(interval_s)
    return False, f"{last_msg} (timeout {timeout_s}s)"


def run_smoke(
    ssh: RouterSSH,
    cfg: dict[str, Any],
    log: Callable[[str], None] | None = None,
) -> SmokeResult:
    msgs: list[str] = []
    ok_all = True

    services = cfg.get("services", {})

    _, out, _ = ssh.exec(
        "D=$(ls /mnt/usb*/mi_docker/docker-binaries/docker 2>/dev/null | head -1); "
        "if [ -x \"$D\" ]; then "
        "  if [ -f /mnt/usb*/opt/docker-cli/compose-env.sh ]; then "
        "    . /mnt/usb*/opt/docker-cli/compose-env.sh 2>/dev/null || true; "
        "  fi; "
        "  \"$D\" compose version 2>&1 || true; "
        "else echo NO_DOCKER_BIN; fi"
    )
    comp_line = out.strip().splitlines()[0] if out.strip() else "UNKNOWN"
    msgs.append(f"compose: {comp_line[:200]}")

    _, out2, _ = ssh.exec(
        "D=$(ls /mnt/usb*/mi_docker/docker-binaries/docker 2>/dev/null | head -1); "
        "[ -x \"$D\" ] && \"$D\" ps --format '{{.Names}} {{.Status}}' 2>&1 | head -20"
    )
    msgs.append(f"docker ps:\n{out2.strip()}")

    sx = services.get("xray_server", {})
    if sx.get("enabled", True):
        port = int(cfg.get("xray", {}).get("inbound", {}).get("port", 443))
        ok, m = _wait_tcp(ssh, "127.0.0.1", port, log=log)
        msgs.append(m)
        ok_all = ok_all and ok

    mh = services.get("mihomo", {})
    if mh.get("enabled", True):
        for p in (mh.get("socks_port", 7890), mh.get("redir_port", 7891)):
            ok, m = _wait_tcp(ssh, "127.0.0.1", int(p), log=log)
            msgs.append(m)
            ok_all = ok_all and ok

    ts = services.get("torrserver", {})
    if ts.get("enabled", True):
        p = int(ts.get("port", 8090))
        ok, m = _wait_tcp(ssh, "127.0.0.1", p, log=log)
        msgs.append(m)
        ok_all = ok_all and ok

    md = services.get("metacubexd", {})
    if md.get("enabled", True):
        p = int(md.get("port", 9099))
        ok, m = _wait_tcp(ssh, "127.0.0.1", p, log=log)
        msgs.append(m)
        ok_all = ok_all and ok

    return SmokeResult(ok=ok_all, messages=msgs)
