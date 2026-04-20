from __future__ import annotations

import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from xiaomi_router.ssh_util import RouterSSH


@dataclass
class SmokeResult:
    ok: bool
    messages: list[str]


def _check_internet(ssh: RouterSSH) -> tuple[bool, str]:
    """Проверяет IP-связность с интернетом через ping до 1.1.1.1."""
    _, out, _ = ssh.exec("ping -c2 -W3 1.1.1.1 >/dev/null 2>&1 && echo OK || echo FAIL")
    ok = "OK" in (out.strip().splitlines()[-1] if out.strip() else "FAIL")
    return ok, f"[сеть] internet (ping 1.1.1.1) -> {'OK' if ok else 'FAIL'}"


def _check_dns(ssh: RouterSSH) -> tuple[bool, str]:
    """Проверяет DNS-резолвинг через локальный dnsmasq (127.0.0.1)."""
    _, out, _ = ssh.exec(
        "if command -v nslookup >/dev/null 2>&1; then "
        "  nslookup one.one.one.one 127.0.0.1 >/dev/null 2>&1 && echo OK || echo FAIL; "
        "else "
        "  ping -c1 -W3 one.one.one.one >/dev/null 2>&1 && echo OK || echo FAIL; "
        "fi"
    )
    ok = "OK" in (out.strip().splitlines()[-1] if out.strip() else "FAIL")
    return ok, f"[сеть] dns (one.one.one.one) -> {'OK' if ok else 'FAIL'}"


def _check_tcp(
    ssh: RouterSSH,
    host: str,
    port: int,
    *,
    service_label: str | None = None,
) -> tuple[bool, str]:
    script = (
        f"netstat -tln 2>/dev/null | grep -Eq '[:.]({port})\\b' "
        "&& echo OK || echo FAIL"
    )
    _, out, _ = ssh.exec(script)
    line = out.strip().splitlines()[-1] if out.strip() else "FAIL"
    prefix = f"[{service_label}] " if service_label else ""
    return ("OK" in line), f"{prefix}tcp {host}:{port} -> {line}"


def _wait_tcp(
    ssh: RouterSSH,
    host: str,
    port: int,
    timeout_s: int = 10,
    interval_s: int = 3,
    log: Callable[[str], None] | None = None,
    *,
    service_label: str | None = None,
) -> tuple[bool, str]:
    prefix = f"[{service_label}] " if service_label else ""
    if log is not None:
        log(f"{prefix}проверка tcp {host}:{port} (таймаут {timeout_s}s)")
    deadline = time.time() + timeout_s
    last_msg = "FAIL"
    started = time.time()
    while time.time() < deadline:
        ok, msg = _check_tcp(ssh, host, port, service_label=service_label)
        last_msg = msg
        if ok:
            return True, f"{msg} (ready)"
        if log is not None:
            elapsed = int(time.time() - started)
            log(f"{prefix}ожидание tcp {host}:{port}... {elapsed}/{timeout_s}s")
        time.sleep(interval_s)
    return False, f"{last_msg} (timeout {timeout_s}s)"


def _tail_container_logs(ssh: RouterSSH, container_name: str, tail: int = 30) -> str:
    """
    Пытается получить последние строки docker logs с роутера.
    Используется как диагностика при падении smoke.
    """
    cn = shlex.quote(container_name)
    script = (
        "D=$(ls /mnt/usb*/mi_docker/docker-binaries/docker 2>/dev/null | head -1); "
        "if [ -x \"$D\" ]; then "
        f"  \"$D\" logs --tail {int(tail)} {cn} 2>&1 || true; "
        "else echo NO_DOCKER_BIN; fi"
    )
    _, out, _ = ssh.exec(script)
    return out.strip()


def _container_state_hint(ssh: RouterSSH, container_name: str) -> str:
    """Краткий статус контейнера (docker ps -a / inspect) для локализации сбоя."""
    qc = shlex.quote(container_name)
    script = (
        "D=$(ls /mnt/usb*/mi_docker/docker-binaries/docker 2>/dev/null | head -1); "
        "if [ ! -x \"$D\" ]; then echo NO_DOCKER_BIN; exit 0; fi; "
        f"C={qc}; "
        f"\"$D\" ps -a --filter name=\"$C\" --format '{{{{.Names}}}}\\t{{{{.Status}}}}' 2>&1 | head -6; "
        "echo '---'; "
        f"\"$D\" inspect -f 'status={{{{.State.Status}}}} exit={{{{.State.ExitCode}}}} "
        f"oom={{{{.State.OOMKilled}}}} err={{{{.State.Error}}}}' \"$C\" 2>&1 | head -3"
    )
    _, out, _ = ssh.exec(script)
    return out.strip()


def _append_container_diagnostics(
    ssh: RouterSSH,
    service_label: str,
    container_name: str,
    msgs: list[str],
    diagnosed: set[str],
) -> None:
    if container_name in diagnosed:
        return
    diagnosed.add(container_name)
    hint = _container_state_hint(ssh, container_name)
    if hint and hint != "NO_DOCKER_BIN":
        msgs.append(
            f"[{service_label}] состояние контейнера {container_name}:\n{hint}"
        )
    logs = _tail_container_logs(ssh, container_name)
    if logs:
        msgs.append(
            f"[{service_label}] docker logs {container_name} (tail):\n{logs}"
        )


def run_smoke(
    ssh: RouterSSH,
    cfg: dict[str, Any],
    log: Callable[[str], None] | None = None,
) -> SmokeResult:
    msgs: list[str] = []
    ok_all = True
    diagnosed_containers: set[str] = set()

    services = cfg.get("services", {})
    _agh = cfg.get("adguardhome")
    agh_app = _agh if isinstance(_agh, dict) else {}

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
    msgs.append(f"[docker] compose: {comp_line[:200]}")

    _, out2, _ = ssh.exec(
        "D=$(ls /mnt/usb*/mi_docker/docker-binaries/docker 2>/dev/null | head -1); "
        "[ -x \"$D\" ] && \"$D\" ps --format '{{.Names}} {{.Status}}' 2>&1 | head -20"
    )
    msgs.append(f"[docker] docker ps:\n{out2.strip()}")

    inet_ok, inet_msg = _check_internet(ssh)
    msgs.append(inet_msg)

    dns_ok, dns_msg = _check_dns(ssh)
    msgs.append(dns_msg)

    sx = services.get("xray_server", {})
    if sx.get("enabled", True):
        port = int(cfg.get("xray", {}).get("inbound", {}).get("port", 443))
        label = "Xray (VLESS inbound)"
        ok, m = _wait_tcp(
            ssh, "127.0.0.1", port, log=log, service_label=label
        )
        msgs.append(m)
        if not ok:
            cname = str(sx.get("container_name", "xray-server"))
            _append_container_diagnostics(
                ssh, label, cname, msgs, diagnosed_containers
            )
        ok_all = ok_all and ok

    mh = services.get("mihomo", {})
    if mh.get("enabled", True):
        label = "mihomo"
        cname = str(mh.get("container_name", "mihomo"))
        for p in (mh.get("socks_port", 7890), mh.get("redir_port", 7891)):
            ok, m = _wait_tcp(
                ssh,
                "127.0.0.1",
                int(p),
                log=log,
                service_label=label,
            )
            msgs.append(m)
            if not ok:
                _append_container_diagnostics(
                    ssh, label, cname, msgs, diagnosed_containers
                )
            ok_all = ok_all and ok

    v2 = services.get("v2raya", {})
    if v2.get("enabled", False):
        label = "v2raya"
        cname = str(v2.get("container_name", "v2raya"))
        ok, m = _wait_tcp(
            ssh,
            "127.0.0.1",
            int(v2.get("port", 2017)),
            log=log,
            service_label=label,
        )
        msgs.append(m)
        if not ok:
            _append_container_diagnostics(
                ssh, label, cname, msgs, diagnosed_containers
            )
        ok_all = ok_all and ok

        routing = cfg.get("routing") if isinstance(cfg.get("routing"), dict) else {}
        proxy_client = str(cfg.get("proxy_client", "mihomo")).strip()
        if routing.get("apply_iptables", False) and proxy_client == "v2raya":
            redir_port = int(v2.get("redir_port", 52345))
            redir_ok, redir_msg = _wait_tcp(
                ssh,
                "127.0.0.1",
                redir_port,
                log=log,
                service_label=label,
            )
            if redir_ok:
                msgs.append(redir_msg)
            else:
                msgs.append(
                    f"[{label}] WARN: transparent redir-порт {redir_port} не готов "
                    "(проверьте настройки transparent proxy в v2rayA); "
                    "smoke не считаю проваленным только из-за этого"
                )

    ts = services.get("torrserver", {})
    if ts.get("enabled", True):
        p = int(ts.get("port", 8090))
        label = "TorrServer"
        ok, m = _wait_tcp(
            ssh,
            "127.0.0.1",
            p,
            timeout_s=60,
            log=log,
            service_label=label,
        )
        msgs.append(m)
        if not ok:
            name = str(ts.get("container_name", "torrserver"))
            _append_container_diagnostics(
                ssh, label, name, msgs, diagnosed_containers
            )
        ok_all = ok_all and ok

    md = services.get("metacubexd", {})
    if md.get("enabled", True):
        p = int(md.get("port", 9099))
        label = "Metacubexd (dashboard)"
        ok, m = _wait_tcp(
            ssh, "127.0.0.1", p, log=log, service_label=label
        )
        msgs.append(m)
        if not ok:
            name = str(md.get("container_name", "metacubexd"))
            _append_container_diagnostics(
                ssh, label, name, msgs, diagnosed_containers
            )
        ok_all = ok_all and ok

    raw_svc = services.get("adguardhome")
    agh_svc = raw_svc if isinstance(raw_svc, dict) else {}
    if agh_svc.get("enabled", True):
        dns_port = int(agh_app.get("dns_port", 5353))
        admin_port = int(agh_app.get("admin_port", 3000))
        label = "AdGuard Home"
        cname = str(agh_svc.get("container_name", "adguardhome"))
        for p in (dns_port, admin_port):
            ok, m = _wait_tcp(
                ssh,
                "127.0.0.1",
                p,
                log=log,
                service_label=label,
            )
            msgs.append(m)
            if not ok:
                _append_container_diagnostics(
                    ssh, label, cname, msgs, diagnosed_containers
                )
            ok_all = ok_all and ok

    if not ok_all:
        if not inet_ok:
            msgs.append(
                "[сеть] Подсказка: без связи с интернетом контейнеры часто не поднимаются "
                "(pull образов, upstream DNS). Проверьте WAN и маршрут по умолчанию."
            )
        elif not dns_ok:
            msgs.append(
                "[сеть] Подсказка: при сбое DNS проверьте dnsmasq (форвард на AdGuard Home "
                "или на mihomo 127.0.0.1:порт из mihomo.dns.listen) и что соответствующий "
                "сервис слушает порт."
            )

    return SmokeResult(ok=ok_all, messages=msgs)
