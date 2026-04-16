from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from xiaomi_router.backup import create_backup, rollback
from xiaomi_router.config_loader import require_router_password
from xiaomi_router.paths import rendered_dir
from xiaomi_router.render import render_all
from xiaomi_router.setup_extra import (
    ensure_compose_with_optional_entware,
    ensure_usb_shell_env,
    remote_compose_env,
)
from xiaomi_router.smoke import run_smoke
from xiaomi_router.ssh_util import RouterSSH

_Noop: Callable[[str], None] = lambda _: None


def _stack_path(cfg: dict[str, Any], usb: str) -> str:
    rel = cfg.get("stack", {}).get("relative_dir", "stack")
    return f"{usb.rstrip('/')}/{rel}"


def _startup_paths(cfg: dict[str, Any]) -> tuple[str, str]:
    st = cfg.get("startup", {})
    base = str(st.get("base_dir", "/data/startup")).strip()
    if not base.startswith("/"):
        raise ValueError(
            "startup.base_dir должен быть абсолютным путём (например '/data/startup')"
        )
    sub = st.get("autoruns_subdir", "autoruns")
    return base, f"{base.rstrip('/')}/{sub}"



def apply_uci_firewall_and_docker_fix(ssh: RouterSSH, cfg: dict[str, Any]) -> None:
    startup_base, _ = _startup_paths(cfg)
    cmds = [
        "uci -q delete firewall.docker_autorun 2>/dev/null || true",
        "uci set firewall.docker_autorun=include",
        "uci set firewall.docker_autorun.type='script'",
        f"uci set firewall.docker_autorun.path='{startup_base}/startup.sh'",
        "uci set firewall.docker_autorun.enabled='1'",
        "uci set firewall.docker_autorun.reload='1'",
        "uci commit firewall",
    ]
    ssh.exec_text("; ".join(cmds))

    ssh.exec_text(
        r"grep -q \"list authorization_plugins 'opa-docker-authz'\" /etc/config/mi_docker 2>/dev/null && "
        r"sed -i \"s/list authorization_plugins 'opa-docker-authz'/list authorization_plugins ''/\" "
        r"/etc/config/mi_docker || true"
    )

    ssh.exec_text("/etc/init.d/mi_docker start 2>/dev/null || true")


def apply_docker_registry_mirrors(
    ssh: RouterSSH,
    cfg: dict[str, Any],
    log: Callable[[str], None] = _Noop,
) -> None:
    mirrors = cfg.get("docker", {}).get("registry_mirrors", [])
    if not mirrors:
        return

    # Этап применяем только если зеркала уже были настроены на роутере.
    # Это позволяет не трогать стабильные установки, где зеркала не используются.
    _, current_raw, _ = ssh.exec("uci show mi_docker.globals.registry_mirrors 2>/dev/null || true")
    current_lines = [ln.strip() for ln in current_raw.splitlines() if ln.strip()]
    if not current_lines:
        log("      registry_mirrors на роутере не настроены — пропускаю применение (по политике).")
        return

    log("      Настройка registry_mirrors в /etc/config/mi_docker...")
    validated: list[str] = []
    for mirror in mirrors:
        m = str(mirror).strip()
        if not m:
            continue
        parsed = urlparse(m)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Невалидный registry_mirror URL: {m}")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError(
                "registry_mirror должен быть origin без path/query/fragment: "
                f"{m}"
            )
        validated.append(m.rstrip("/"))

    if not validated:
        log("      registry_mirrors пустой после валидации — пропускаю.")
        return

    # Примитивная нормализация текущего списка из UCI для сравнения.
    current_vals: list[str] = []
    for ln in current_lines:
        # Пример: mi_docker.globals.registry_mirrors='https://a' 'https://b'
        if "=" not in ln:
            continue
        rhs = ln.split("=", 1)[1].strip()
        # Соберём все '...'
        parts = []
        buf = ""
        in_q = False
        for ch in rhs:
            if ch == "'":
                in_q = not in_q
                if not in_q and buf:
                    parts.append(buf)
                    buf = ""
                continue
            if in_q:
                buf += ch
        current_vals.extend([p.rstrip("/") for p in parts if p.strip()])

    if sorted(current_vals) == sorted([m.rstrip("/") for m in validated]):
        log("      registry_mirrors уже установлены — перезапуск mi_docker не требуется.")
        return

    cmds = ["uci -q delete mi_docker.globals.registry_mirrors 2>/dev/null || true"]
    for m in validated:
        cmds.append(f"uci add_list mi_docker.globals.registry_mirrors='{m}'")
    cmds.append("uci commit mi_docker")
    ssh.exec_text("; ".join(cmds))

    _, out, _ = ssh.exec("uci show mi_docker.globals | grep registry_mirrors || true")
    if out.strip():
        for line in out.strip().splitlines():
            log(f"      {line}")

    log("      Перезапуск mi_docker для применения зеркал...")
    ssh.exec_text("/etc/init.d/mi_docker restart 2>/dev/null || true")
    time.sleep(5)
    log("      mi_docker перезапущен.")


def apply_dnsmasq_forward_to_adguardhome(
    ssh: RouterSSH,
    cfg: dict[str, Any],
    log: Callable[[str], None] = _Noop,
) -> None:
    agh = cfg.get("services", {}).get("adguardhome", {})
    dns_port = int(agh.get("dns_port", 5353))
    dns_host = str(agh.get("dns_host", "127.0.0.1")).strip() or "127.0.0.1"
    dns_upstream = f"{dns_host}#{dns_port}"

    if not agh.get("enabled", True):
        # Самовосстановление: если раньше уже включали AGH-forward,
        # при выключенном AGH возвращаем dnsmasq к системным резолверам.
        _, out, _ = ssh.exec("uci show dhcp.@dnsmasq[0] | grep -E 'server=|noresolv=' || true")
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        has_local_forward = any(dns_upstream in ln for ln in lines)
        has_noresolv = any("noresolv='1'" in ln for ln in lines)
        if has_local_forward or has_noresolv:
            log("      AdGuard Home отключен — восстанавливаю штатный dnsmasq (без local forward).")
            cmds = [
                "uci -q delete dhcp.@dnsmasq[0].server 2>/dev/null || true",
                "uci -q delete dhcp.@dnsmasq[0].noresolv 2>/dev/null || true",
                "uci commit dhcp",
            ]
            ssh.exec_text("; ".join(cmds))
            ssh.exec_text("/etc/init.d/dnsmasq restart 2>/dev/null || true")
            log("      dnsmasq восстановлен к системным upstream.")
        else:
            log("      AdGuard Home отключен — dnsmasq уже в штатном режиме.")
        return

    log(f"      Настройка dnsmasq -> {dns_upstream} через UCI...")
    cmds = [
        "uci -q delete dhcp.@dnsmasq[0].server 2>/dev/null || true",
        "uci set dhcp.@dnsmasq[0].noresolv='1'",
        f"uci add_list dhcp.@dnsmasq[0].server='{dns_upstream}'",
        "uci commit dhcp",
    ]
    ssh.exec_text("; ".join(cmds))
    _, out, _ = ssh.exec("uci show dhcp.@dnsmasq[0] | grep -E 'server=|noresolv=' || true")
    if out.strip():
        for line in out.strip().splitlines():
            log(f"      {line}")
    ssh.exec_text("/etc/init.d/dnsmasq restart 2>/dev/null || true")
    log("      dnsmasq перезапущен.")


def deploy(
    cfg: dict[str, Any],
    *,
    skip_smoke: bool = False,
    skip_backup: bool = False,
    rollback_on_smoke_fail: bool = True,
    log: Callable[[str], None] = _Noop,
) -> dict[str, Any] | None:
    router = cfg["router"]
    pwd = require_router_password(cfg)
    ssh = RouterSSH(
        host=str(router["host"]),
        password=pwd,
        port=int(router.get("ssh_port", 22)),
        username=str(router.get("ssh_user", "root")),
    )
    meta: dict[str, Any] | None = None
    try:
        log(f"[1/6] Подключение к {router['host']}...")
        usb = ssh.usb_mount_from_router(cfg.get("usb", {}).get("mount_path"))
        log(f"      USB: {usb}")
        stack = _stack_path(cfg, usb)
        startup_base, _ = _startup_paths(cfg)

        if not skip_backup:
            log("[2/6] Создание бэкапа...")
            meta = create_backup(ssh, cfg, usb_mount=usb, stack_path=stack, startup_base=startup_base)
            log(f"      Бэкап: {meta['name']}")
        else:
            log("[2/6] Бэкап пропущен (--skip-backup).")

        log("[3/6] Рендер шаблонов...")
        ssh.exec_text(f"mkdir -p '{startup_base}/autoruns'")
        out = render_all(cfg, usb)
        log(f"      Артефакты: {out}")

        log("[4/6] Загрузка файлов на роутер...")
        ssh.exec_text(
            f"mkdir -p '{stack}/configs/xray' '{stack}/configs/mihomo' "
            f"'{stack}/configs/adguardhome/conf' '{stack}/configs/adguardhome/work' "
            f"'{stack}/mihomo'"
        )
        uploads = [
            (out / "docker-compose.yml",                          f"{stack}/docker-compose.yml",                                   None),
            (out / "configs/xray/config.json",                    f"{stack}/configs/xray/config.json",                             None),
            (out / "configs/mihomo/config.yaml",                  f"{stack}/configs/mihomo/config.yaml",                           None),
            (out / "configs/adguardhome/conf/AdGuardHome.yaml",   f"{stack}/configs/adguardhome/conf/AdGuardHome.yaml",            None),
            (out / "mihomo/mihomo-routing.sh",                    f"{stack}/mihomo/mihomo-routing.sh",                             0o755),
            (out / "mihomo/rollback.sh",                          f"{stack}/mihomo/rollback.sh",                                   0o755),
            (out / "startup/startup.sh",                          f"{startup_base}/startup.sh",                                    0o755),
            (out / "startup/autoruns/010-start-docker.sh",        f"{startup_base}/autoruns/010-start-docker.sh",                  0o755),
            (out / "startup/autoruns/020-mihomo-routing.sh",      f"{startup_base}/autoruns/020-mihomo-routing.sh",                0o755),
        ]
        for local, remote, mode in uploads:
            log(f"      → {remote}")
            kwargs: dict[str, Any] = {}
            if mode is not None:
                kwargs["mode"] = mode
            ssh.upload_file(local, remote, **kwargs)

        log("[5/6] Применение UCI firewall и перезапуск docker...")
        apply_uci_firewall_and_docker_fix(ssh, cfg)
        apply_docker_registry_mirrors(ssh, cfg, log=log)
        ensure_usb_shell_env(ssh, usb, log=log)
        ensure_compose_with_optional_entware(ssh, usb, log=log)

        env = remote_compose_env(usb)
        code = ssh.exec_streaming(
            f"{env}; cd '{stack}' && docker compose up -d 2>&1",
            log=lambda line: log(f"      {line}"),
            timeout=300,
        )
        if code != 0:
            raise RuntimeError(f"docker compose up завершился с кодом {code}")

        # AdGuard Home читает конфиг при старте. При обновлении AdGuardHome.yaml
        # безопаснее перезапустить контейнер, чтобы избежать работы со старым in-memory cfg.
        if cfg.get("services", {}).get("adguardhome", {}).get("enabled", True):
            ssh.exec_text(f"{env}; cd '{stack}' && docker compose restart adguardhome 2>/dev/null || true")

        apply_dnsmasq_forward_to_adguardhome(ssh, cfg, log=log)

        routing = cfg.get("routing", {})
        if routing.get("apply_iptables", False) or routing.get("block_quic", False):
            log("      Применяю mihomo routing правила (start)...")
            ssh.exec_streaming(
                f"sh '{stack}/mihomo/mihomo-routing.sh' start 2>&1",
                log=lambda line: log(f"      {line}"),
                timeout=60,
            )

        if not skip_smoke:
            log("[6/6] Smoke-проверки...")
            res = run_smoke(ssh, cfg, log=lambda line: log(f"      {line}"))
            for msg in res.messages:
                log(f"      {msg}")
            if not res.ok:
                if rollback_on_smoke_fail:
                    log("      Smoke не прошёл — откат...")
                    ssh.exec_text(
                        f"{env}; cd '{stack}' && docker compose down 2>/dev/null || true"
                    )
                    if meta:
                        rollback(ssh, meta)
                else:
                    log("      Smoke не прошёл — откат отключён, оставляю текущий state.")
                raise RuntimeError("Smoke failed:\n" + "\n".join(res.messages))
            log("      Smoke OK.")
        else:
            log("[6/6] Smoke пропущен (--skip-smoke).")

        return meta
    finally:
        ssh.close()


def pull_configs(
    cfg: dict[str, Any],
    dest: Path,
) -> None:
    router = cfg["router"]
    pwd = require_router_password(cfg)
    ssh = RouterSSH(
        host=str(router["host"]),
        password=pwd,
        port=int(router.get("ssh_port", 22)),
        username=str(router.get("ssh_user", "root")),
    )
    try:
        usb = ssh.usb_mount_from_router(cfg.get("usb", {}).get("mount_path"))
        stack = _stack_path(cfg, usb)
        dest.mkdir(parents=True, exist_ok=True)
        for rel in (
            "configs/xray/config.json",
            "configs/mihomo/config.yaml",
            "configs/adguardhome/conf/AdGuardHome.yaml",
            "docker-compose.yml",
            "mihomo/mihomo-routing.sh",
        ):
            rpath = f"{stack}/{rel}"
            if not ssh.remote_path_exists(rpath):
                continue
            data = ssh.download_bytes(rpath)
            lp = dest / rel
            lp.parent.mkdir(parents=True, exist_ok=True)
            lp.write_bytes(data)
    finally:
        ssh.close()


def push_rendered_only(cfg: dict[str, Any], local_rendered: Path | None = None) -> None:
    out = local_rendered or rendered_dir()
    router = cfg["router"]
    pwd = require_router_password(cfg)
    ssh = RouterSSH(
        host=str(router["host"]),
        password=pwd,
        port=int(router.get("ssh_port", 22)),
        username=str(router.get("ssh_user", "root")),
    )
    try:
        usb = ssh.usb_mount_from_router(cfg.get("usb", {}).get("mount_path"))
        stack = _stack_path(cfg, usb)
        ssh.upload_file(out / "docker-compose.yml", f"{stack}/docker-compose.yml")
        ssh.upload_file(out / "configs/xray/config.json", f"{stack}/configs/xray/config.json")
        ssh.upload_file(out / "configs/mihomo/config.yaml", f"{stack}/configs/mihomo/config.yaml")
        ssh.upload_file(
            out / "configs/adguardhome/conf/AdGuardHome.yaml",
            f"{stack}/configs/adguardhome/conf/AdGuardHome.yaml",
        )
        ssh.upload_file(out / "mihomo/mihomo-routing.sh", f"{stack}/mihomo/mihomo-routing.sh", mode=0o755)
        ssh.upload_file(out / "mihomo/rollback.sh", f"{stack}/mihomo/rollback.sh", mode=0o755)
        ensure_compose_with_optional_entware(ssh, usb)
        env = remote_compose_env(usb)
        ssh.exec_text(f"{env}; cd '{stack}' && docker compose up -d 2>&1")
        if cfg.get("services", {}).get("adguardhome", {}).get("enabled", True):
            ssh.exec_text(f"{env}; cd '{stack}' && docker compose restart adguardhome 2>/dev/null || true")
        apply_dnsmasq_forward_to_adguardhome(ssh, cfg)
    finally:
        ssh.close()


def cmd_rollback(cfg: dict[str, Any], meta_json: Path) -> None:
    import json

    router = cfg["router"]
    pwd = require_router_password(cfg)
    meta = json.loads(meta_json.read_text(encoding="utf-8"))
    ssh = RouterSSH(
        host=str(router["host"]),
        password=pwd,
        port=int(router.get("ssh_port", 22)),
        username=str(router.get("ssh_user", "root")),
    )
    try:
        rollback(ssh, meta)
    finally:
        ssh.close()
