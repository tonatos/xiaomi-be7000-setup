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


def _xray_inbound_port(cfg: dict[str, Any]) -> int:
    xray = cfg.get("xray") if isinstance(cfg.get("xray"), dict) else {}
    inbound = xray.get("inbound") if isinstance(xray.get("inbound"), dict) else {}
    try:
        port = int(inbound.get("port", 443))
    except (TypeError, ValueError):
        port = 443
    if port < 1 or port > 65535:
        return 443
    return port


def _compose_up_then_restart(
    ssh: RouterSSH,
    env: str,
    stack: str,
    log: Callable[[str], None],
) -> None:
    """Создаёт/обновляет контейнеры и перезапускает их, чтобы подхватить новые файлы в bind-mount.

    ``--remove-orphans`` убирает контейнеры сервисов, удалённых из compose.

    При неизменном compose-файле ``docker compose up -d`` часто не трогает уже
    запущенные контейнеры; процессы (xray, mihomo и др.) продолжают работать со
    старым конфигом в памяти. ``compose restart`` перечитывает смонтированные конфиги.
    """
    code = ssh.exec_streaming(
        f"{env}; cd '{stack}' && docker compose up -d --remove-orphans 2>&1",
        log=lambda line: log(f"      {line}"),
        timeout=300,
    )
    if code != 0:
        raise RuntimeError(f"docker compose up завершился с кодом {code}")
    log(
        "      docker compose restart — подхват обновлённых конфигов (bind-mount)..."
    )
    code = ssh.exec_streaming(
        f"{env}; cd '{stack}' && docker compose restart 2>&1",
        log=lambda line: log(f"      {line}"),
        timeout=180,
    )
    if code != 0:
        raise RuntimeError(f"docker compose restart завершился с кодом {code}")


def ensure_opa_docker_authz_disabled(ssh: RouterSSH) -> bool:
    """Убрать opa-docker-authz из UCI mi_docker (policy denied без этого).

    Правка через UCI (del_list + commit), иначе последующий ``uci commit mi_docker``
    (зеркала и др.) может перезаписать файл и вернуть плагин.

    Перезапуск mi_docker только если плагин ещё был в конфиге.
    Возвращает True, если демон трогали (restart/start для применения правки).
    """
    _, probe, _ = ssh.exec(
        "uci show mi_docker 2>/dev/null | grep -qE 'authorization_plugins.*opa-docker-authz' && echo NEED || "
        "grep -q 'opa-docker-authz' /etc/config/mi_docker 2>/dev/null && echo NEED || true"
    )
    if "NEED" not in probe:
        return False

    #1) Штатно через UCI (тот же пакет/секция, что и registry_mirrors).
    _, uci_out, _ = ssh.exec(
        "if uci -q del_list mi_docker.globals.authorization_plugins='opa-docker-authz'; then "
        "uci commit mi_docker; echo UCI_OK; "
        "elif uci -q del_list mi_docker.@globals[0].authorization_plugins='opa-docker-authz'; then "
        "uci commit mi_docker; echo UCI_OK; "
        "else echo UCI_SKIP; fi"
    )
    if "UCI_OK" not in uci_out:
        # 2) Запасной sed: пробелы/табы в начале строки, одинарные или двойные кавычки.
        ssh.exec_text(
            "sed -i "
            "-e \"s/^[[:space:]]*list[[:space:]]*authorization_plugins[[:space:]]*'opa-docker-authz'[[:space:]]*$/list authorization_plugins ''/\" "
            "-e 's/^[[:space:]]*list[[:space:]]*authorization_plugins[[:space:]]*\"opa-docker-authz\"[[:space:]]*$/list authorization_plugins \"\"/g' "
            "/etc/config/mi_docker"
        )

    ssh.exec_text(
        "/etc/init.d/mi_docker restart 2>/dev/null || /etc/init.d/mi_docker start 2>/dev/null || true"
    )
    return True


def apply_uci_firewall_and_docker_fix(ssh: RouterSSH, cfg: dict[str, Any]) -> None:
    startup_base, _ = _startup_paths(cfg)
    services = cfg.get("services") if isinstance(cfg.get("services"), dict) else {}
    xray_svc = (
        services.get("xray_server")
        if isinstance(services.get("xray_server"), dict)
        else {}
    )
    xray_enabled = bool(xray_svc.get("enabled", True))
    cmds = [
        "uci -q delete firewall.docker_autorun 2>/dev/null || true",
        "uci set firewall.docker_autorun=include",
        "uci set firewall.docker_autorun.type='script'",
        f"uci set firewall.docker_autorun.path='{startup_base}/startup.sh'",
        "uci set firewall.docker_autorun.enabled='1'",
        "uci set firewall.docker_autorun.reload='1'",
        "uci -q delete firewall.xray_vless_wan_allow 2>/dev/null || true",
    ]
    if xray_enabled:
        xray_port = _xray_inbound_port(cfg)
        cmds.extend(
            [
                "uci set firewall.xray_vless_wan_allow=rule",
                "uci set firewall.xray_vless_wan_allow.name='Allow-Xray-VLESS-WAN'",
                "uci set firewall.xray_vless_wan_allow.src='wan'",
                "uci set firewall.xray_vless_wan_allow.proto='tcp'",
                f"uci set firewall.xray_vless_wan_allow.dest_port='{xray_port}'",
                "uci set firewall.xray_vless_wan_allow.target='ACCEPT'",
                "uci set firewall.xray_vless_wan_allow.enabled='1'",
            ]
        )
    cmds.append("uci commit firewall")
    ssh.exec_text("; ".join(cmds))
    # Важно: commit меняет конфиг UCI, но не всегда применяет правила в runtime.
    # Для VLESS/WAN это критично — без reload/restart правило может отсутствовать в iptables.
    ssh.exec_text("/etc/init.d/firewall reload 2>/dev/null || /etc/init.d/firewall restart 2>/dev/null || true")

    opa_restarted = ensure_opa_docker_authz_disabled(ssh)
    if not opa_restarted:
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


def _mihomo_dns_upstream_for_dnsmasq(cfg: dict[str, Any]) -> str | None:
    """
    Адрес для UCI dhcp.@dnsmasq[0].server=…#port при network_mode: host у mihomo.
    Возвращает None, если встроенный DNS mihomo выключен (dns.enable: false).
    """
    mihomo_app = cfg.get("mihomo") if isinstance(cfg.get("mihomo"), dict) else {}
    dns = mihomo_app.get("dns") if isinstance(mihomo_app.get("dns"), dict) else {}
    if dns.get("enable") is False:
        return None
    listen = str(dns.get("listen", "0.0.0.0:1053")).strip()
    try:
        port = int(listen.rsplit(":", maxsplit=1)[-1].strip())
    except (ValueError, IndexError):
        port = 1053
    if port < 1 or port > 65535:
        port = 1053
    return f"127.0.0.1#{port}"


def apply_dnsmasq_upstream(
    ssh: RouterSSH,
    cfg: dict[str, Any],
    log: Callable[[str], None] = _Noop,
) -> None:
    """
    Направляет dnsmasq на локальный DNS-стек: AdGuard Home или mihomo (порт из mihomo.dns.listen).
    Если оба не подходят — снимает ранее выставленный нами forward (noresolv/server).
    """
    raw_agh = cfg.get("services", {}).get("adguardhome")
    agh_svc = raw_agh if isinstance(raw_agh, dict) else {}
    raw_mihomo = cfg.get("services", {}).get("mihomo")
    mihomo_svc = raw_mihomo if isinstance(raw_mihomo, dict) else {}
    raw_app = cfg.get("adguardhome")
    raw_mihomo_app = cfg.get("mihomo")
    mihomo_app = raw_mihomo_app if isinstance(raw_mihomo_app, dict) else {}
    raw_routing = cfg.get("routing")
    routing_cfg = raw_routing if isinstance(raw_routing, dict) else {}
    agh_app = raw_app if isinstance(raw_app, dict) else {}
    dns_port = int(agh_app.get("dns_port", 5353))
    dns_host = str(agh_app.get("dns_host", "127.0.0.1")).strip() or "127.0.0.1"
    agh_upstream = f"{dns_host}#{dns_port}"

    mihomo_upstream = _mihomo_dns_upstream_for_dnsmasq(cfg)
    proxy_client = str(cfg.get("proxy_client", "mihomo")).strip()
    mihomo_tun = mihomo_app.get("tun") if isinstance(mihomo_app.get("tun"), dict) else {}
    mihomo_tun_enabled = bool(mihomo_tun.get("enable", False))
    apply_iptables = bool(routing_cfg.get("apply_iptables", False))
    skip_mihomo_dns_forward_for_tun = (
        proxy_client == "mihomo"
        and mihomo_svc.get("enabled", True)
        and mihomo_tun_enabled
        and not apply_iptables
    )

    desired: str | None
    if agh_svc.get("enabled", True):
        desired = agh_upstream
    elif (
        proxy_client == "mihomo"
        and mihomo_svc.get("enabled", True)
        and mihomo_upstream is not None
        and not skip_mihomo_dns_forward_for_tun
    ):
        desired = mihomo_upstream
    else:
        desired = None

    _, out, _ = ssh.exec("uci show dhcp.@dnsmasq[0] | grep -E 'server=|noresolv=' || true")
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    has_noresolv = any("noresolv='1'" in ln for ln in lines)
    has_agh_forward = any(agh_upstream in ln for ln in lines)
    has_mihomo_forward = (
        any(mihomo_upstream in ln for ln in lines) if mihomo_upstream else False
    )

    if desired is None:
        if skip_mihomo_dns_forward_for_tun:
            log(
                "      Пропускаю dnsmasq -> mihomo: при mihomo.tun.enable=true и "
                "routing.apply_iptables=false локальный трафик роутера не проходит через "
                "proxy-path, а fake-ip ломает исходящие подключения."
            )
        if has_agh_forward or has_mihomo_forward or has_noresolv:
            log(
                "      AdGuard Home / mihomo DNS не используются как upstream — "
                "восстанавливаю штатный dnsmasq (без local forward)."
            )
            cmds = [
                "uci -q delete dhcp.@dnsmasq[0].server 2>/dev/null || true",
                "uci -q delete dhcp.@dnsmasq[0].noresolv 2>/dev/null || true",
                "uci commit dhcp",
            ]
            ssh.exec_text("; ".join(cmds))
            ssh.exec_text("/etc/init.d/dnsmasq restart 2>/dev/null || true")
            log("      dnsmasq восстановлен к системным upstream.")
        else:
            log("      dnsmasq уже в штатном режиме (без forward на AGH/mihomo).")
        return

    log(f"      Настройка dnsmasq -> {desired} через UCI...")
    cmds = [
        "uci -q delete dhcp.@dnsmasq[0].server 2>/dev/null || true",
        "uci set dhcp.@dnsmasq[0].noresolv='1'",
        f"uci add_list dhcp.@dnsmasq[0].server='{desired}'",
        "uci commit dhcp",
    ]
    ssh.exec_text("; ".join(cmds))
    _, out2, _ = ssh.exec("uci show dhcp.@dnsmasq[0] | grep -E 'server=|noresolv=' || true")
    if out2.strip():
        for line in out2.strip().splitlines():
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
            f"mkdir -p '{stack}/configs/xray' '{stack}/configs/mihomo-config' "
            f"'{stack}/configs/torrserver/config' "
            f"'{stack}/configs/adguardhome/conf' "
            f"'{stack}/routing'"
        )
        # Runtime-каталоги вынесены вне stack (compose default ../...).
        # Для AGH мигрируем legacy-data из stack/configs/adguardhome/work, если новое место пустое.
        ssh.exec_text(
            f"mkdir -p '{usb}/torrserver-runtime' '{usb}/adguardhome-runtime/work' "
            f"'{usb}/mihomo-runtime' '{usb}/v2raya-runtime'; "
            f"if [ -d '{stack}/configs/adguardhome/work' ] && "
            f"[ -z \"$(ls -A '{usb}/adguardhome-runtime/work' 2>/dev/null)\" ]; then "
            f"cp -a '{stack}/configs/adguardhome/work/.' '{usb}/adguardhome-runtime/work/' 2>/dev/null || true; "
            "fi"
        )
        # Очистить управляемые autorun-скрипты перед заливкой:
        # так не остаются хвосты после переименований/переключений клиента.
        ssh.exec_text(
            f"rm -f {startup_base}/autoruns/* 2>/dev/null || true"
        )
        uploads = [
            (out / "docker-compose.yml",                          f"{stack}/docker-compose.yml",                                   None),
            (out / "configs/xray/config.json",                    f"{stack}/configs/xray/config.json",                             None),
            (out / "configs/mihomo-config/config.yaml",           f"{stack}/configs/mihomo-config/config.yaml",                    None),
            (out / "configs/torrserver/config/settings.json",     f"{stack}/configs/torrserver/config/settings.json",              None),
            (out / "configs/adguardhome/conf/AdGuardHome.yaml",   f"{stack}/configs/adguardhome/conf/AdGuardHome.yaml",            None),
            (out / "routing/lan-routing.sh",                      f"{stack}/routing/lan-routing.sh",                               0o755),
            (out / "routing/rollback.sh",                         f"{stack}/routing/rollback.sh",                                  0o755),
            (out / "startup/startup.sh",                          f"{startup_base}/startup.sh",                                    0o755),
            (out / "startup/autoruns/005-ensure-shell-env.sh",    f"{startup_base}/autoruns/005-ensure-shell-env.sh",              0o755),
            (out / "startup/autoruns/010-start-docker.sh",        f"{startup_base}/autoruns/010-start-docker.sh",                  0o755),
            (out / "startup/autoruns/020-proxy-routing.sh",     f"{startup_base}/autoruns/020-proxy-routing.sh",                 0o755),
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
        _compose_up_then_restart(ssh, env, stack, log=log)

        apply_dnsmasq_upstream(ssh, cfg, log=log)

        # Запускаем lan-routing всегда для конвергенции runtime-состояния:
        # скрипт сам решит, нужно ли применять REDIRECT, и подчистит устаревшие
        # project-managed правила после переключения режимов (tun/redirect).
        if str(cfg.get("proxy_client", "mihomo")).strip() in ("mihomo", "v2raya"):
            log("      Применяю lan-routing (iptables convergence) (start)...")
            ssh.exec_streaming(
                f"sh '{stack}/routing/lan-routing.sh' start 2>&1",
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
                        f"sh '{stack}/routing/lan-routing.sh' stop 2>/dev/null || true"
                    )
                    ssh.exec_text(
                        f"{env}; cd '{stack}' && docker compose down --remove-orphans 2>/dev/null || true"
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
            "configs/mihomo-config/config.yaml",
            "configs/adguardhome/conf/AdGuardHome.yaml",
            "docker-compose.yml",
            "routing/lan-routing.sh",
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
        ssh.upload_file(
            out / "configs/mihomo-config/config.yaml",
            f"{stack}/configs/mihomo-config/config.yaml",
        )
        ssh.upload_file(
            out / "configs/torrserver/config/settings.json",
            f"{stack}/configs/torrserver/config/settings.json",
        )
        ssh.upload_file(
            out / "configs/adguardhome/conf/AdGuardHome.yaml",
            f"{stack}/configs/adguardhome/conf/AdGuardHome.yaml",
        )
        ssh.upload_file(out / "routing/lan-routing.sh", f"{stack}/routing/lan-routing.sh", mode=0o755)
        ssh.upload_file(out / "routing/rollback.sh", f"{stack}/routing/rollback.sh", mode=0o755)
        ensure_opa_docker_authz_disabled(ssh)
        ensure_compose_with_optional_entware(ssh, usb)
        env = remote_compose_env(usb)
        _compose_up_then_restart(ssh, env, stack, log=_Noop)
        apply_dnsmasq_upstream(ssh, cfg)
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
