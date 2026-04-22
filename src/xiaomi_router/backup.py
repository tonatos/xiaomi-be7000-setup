from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from xiaomi_router.ssh_util import RouterSSH


def backups_dir(cfg: dict[str, Any], usb_mount: str) -> str:
    rel = cfg.get("stack", {}).get("backups_relative_dir", "backups")
    return f"{usb_mount.rstrip('/')}/{rel}"


def create_backup(
    ssh: RouterSSH,
    cfg: dict[str, Any],
    *,
    usb_mount: str,
    stack_path: str,
    startup_base: str,
) -> dict[str, Any]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bk = backups_dir(cfg, usb_mount)
    name = f"deploy-{ts}"
    startup_tar = f"{bk}/{name}-startup.tar.gz"
    stack_tar = f"{bk}/{name}-stack.tar.gz"
    fw_path = f"{bk}/{name}.firewall.export"
    dhcp_path = f"{bk}/{name}.dhcp.export"
    meta_path = f"{bk}/{name}.json"

    ssh.exec_text(f"mkdir -p '{bk}'")

    ssh.exec_text(
        f"START='{startup_base}'; REL=\"${{START#/}}\"; "
        f"if [ -d \"$START\" ]; then tar czf '{startup_tar}' -C / \"$REL\"; "
        f"else : > '{startup_tar}'; fi"
    )

    rel_stack = stack_path[len(usb_mount.rstrip("/")) :].lstrip("/")
    if rel_stack:
        ssh.exec_text(
            f"TS_CFG='{rel_stack}/configs/torrserver'; "
            f"AGH_WORK='{rel_stack}/configs/adguardhome/work'; "
            f"MIHOMO_OLD='{rel_stack}/configs/mihomo'; "
            f"V2RAYA_OLD='{rel_stack}/configs/v2raya'; "
            f"if [ -d '{stack_path}' ]; then "
            f"cd '{usb_mount}' && tar czf '{stack_tar}' "
            "--exclude=\"${TS_CFG}/TorrServer-*\" "
            "--exclude=\"${TS_CFG}/torrents\" "
            "--exclude=\"${TS_CFG}/cache\" "
            "--exclude=\"${TS_CFG}/log\" "
            "--exclude=\"${TS_CFG}/*.log\" "
            "--exclude=\"${AGH_WORK}\" "
            "--exclude=\"${AGH_WORK}/*\" "
            "--exclude=\"${MIHOMO_OLD}\" "
            "--exclude=\"${MIHOMO_OLD}/*\" "
            "--exclude=\"${V2RAYA_OLD}\" "
            "--exclude=\"${V2RAYA_OLD}/*\" "
            f"'{rel_stack}'; "
            f"else : > '{stack_tar}'; fi"
        )
    else:
        ssh.exec_text(f": > '{stack_tar}'")

    ssh.exec_text(f"uci export firewall > '{fw_path}' 2>/dev/null || true")
    ssh.exec_text(f"uci export dhcp > '{dhcp_path}' 2>/dev/null || true")

    meta: dict[str, Any] = {
        "name": name,
        "timestamp_utc": ts,
        "tar_startup": startup_tar,
        "tar_stack": stack_tar,
        "firewall_export": fw_path,
        "dhcp_export": dhcp_path,
        "startup_base": startup_base,
        "stack_path": stack_path,
        "usb_mount": usb_mount,
    }
    ssh.upload_bytes(meta_path, json.dumps(meta, indent=2).encode("utf-8"))
    return meta


def rollback(ssh: RouterSSH, meta: dict[str, Any]) -> None:
    tar_s = meta.get("tar_startup")
    tar_st = meta.get("tar_stack")
    fw = meta.get("firewall_export")
    dhcp = meta.get("dhcp_export")
    usb = meta.get("usb_mount", "")
    stack_path = str(meta.get("stack_path", "")).strip()

    # Сначала снимаем runtime-правила iptables, чтобы восстановить интернет даже
    # в случае частичного/неполного rollback по файлам.
    if stack_path:
        ssh.exec_text(
            f"if [ -x '{stack_path}/routing/lan-routing.sh' ]; then "
            f"sh '{stack_path}/routing/lan-routing.sh' stop 2>/dev/null || true; "
            "fi"
        )
        # Legacy-совместимость со старыми стеками до объединения routing-скрипта.
        ssh.exec_text(
            f"if [ -x '{stack_path}/mihomo/mihomo-routing.sh' ]; then "
            f"sh '{stack_path}/mihomo/mihomo-routing.sh' stop 2>/dev/null || true; "
            "fi"
        )
        ssh.exec_text(
            f"if [ -x '{stack_path}/v2raya/v2raya-routing.sh' ]; then "
            f"sh '{stack_path}/v2raya/v2raya-routing.sh' stop 2>/dev/null || true; "
            "fi"
        )

    if tar_s:
        ssh.exec_text(
            f"if [ -s '{tar_s}' ]; then tar xzf '{tar_s}' -C / 2>/dev/null || true; fi"
        )
    if tar_st and usb:
        ssh.exec_text(
            f"if [ -s '{tar_st}' ]; then tar xzf '{tar_st}' -C '{usb}' 2>/dev/null || true; fi"
        )
    if fw:
        ssh.exec_text(
            f"if [ -s '{fw}' ]; then "
            f"( uci import -q < '{fw}' && uci commit firewall ) 2>/dev/null || true; "
            f"/etc/init.d/firewall reload 2>/dev/null || true; fi"
        )
    if dhcp:
        ssh.exec_text(
            f"if [ -s '{dhcp}' ]; then "
            f"( uci import -q < '{dhcp}' && uci commit dhcp ) 2>/dev/null || true; "
            f"/etc/init.d/dnsmasq restart 2>/dev/null || true; fi"
        )
