from __future__ import annotations

from pathlib import Path
from typing import Any

from xiaomi_router.backup import create_backup, rollback
from xiaomi_router.config_loader import require_router_password
from xiaomi_router.paths import rendered_dir
from xiaomi_router.render import render_all
from xiaomi_router.smoke import run_smoke
from xiaomi_router.ssh_util import RouterSSH


def _stack_path(cfg: dict[str, Any], usb: str) -> str:
    rel = cfg.get("stack", {}).get("relative_dir", "stack")
    return f"{usb.rstrip('/')}/{rel}"


def _startup_paths(cfg: dict[str, Any]) -> tuple[str, str]:
    st = cfg.get("startup", {})
    base = st.get("base_dir", "/data/startup")
    sub = st.get("autoruns_subdir", "autoruns")
    return base, f"{base.rstrip('/')}/{sub}"


def _remote_compose_env(usb: str) -> str:
    return (
        f"export PATH='{usb}/mi_docker/docker-binaries:'\"$PATH\"; "
        f"if [ -f '{usb}/opt/usb-env.sh' ]; then . '{usb}/opt/usb-env.sh'; fi; "
        f"if [ -f '{usb}/opt/docker-cli/compose-env.sh' ]; then "
        f". '{usb}/opt/docker-cli/compose-env.sh'; fi"
    )


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


def deploy(
    cfg: dict[str, Any],
    *,
    skip_smoke: bool = False,
    skip_backup: bool = False,
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
        usb = ssh.usb_mount_from_router(cfg.get("usb", {}).get("mount_path"))
        stack = _stack_path(cfg, usb)
        startup_base, _ = _startup_paths(cfg)

        if not skip_backup:
            meta = create_backup(ssh, cfg, usb_mount=usb, stack_path=stack, startup_base=startup_base)

        ssh.exec_text(f"mkdir -p '{startup_base}/autoruns'")

        out = render_all(cfg, usb)
        ssh.exec_text(f"mkdir -p '{stack}/configs/xray' '{stack}/configs/mihomo' '{stack}/mihomo'")

        ssh.upload_file(out / "docker-compose.yml", f"{stack}/docker-compose.yml")
        ssh.upload_file(out / "configs/xray/config.json", f"{stack}/configs/xray/config.json")
        ssh.upload_file(out / "configs/mihomo/config.yaml", f"{stack}/configs/mihomo/config.yaml")
        ssh.upload_file(out / "mihomo/mihomo-routing.sh", f"{stack}/mihomo/mihomo-routing.sh", mode=0o755)

        ssh.upload_file(out / "startup/startup.sh", f"{startup_base}/startup.sh", mode=0o755)
        ssh.upload_file(
            out / "startup/autoruns/010-start-docker.sh",
            f"{startup_base}/autoruns/010-start-docker.sh",
            mode=0o755,
        )
        ssh.upload_file(
            out / "startup/autoruns/020-mihomo-routing.sh",
            f"{startup_base}/autoruns/020-mihomo-routing.sh",
            mode=0o755,
        )

        apply_uci_firewall_and_docker_fix(ssh, cfg)

        env = _remote_compose_env(usb)
        code, cout, cerr = ssh.exec(
            f"{env}; cd '{stack}' && docker compose up -d 2>&1",
            timeout=300,
        )
        if code != 0:
            raise RuntimeError(f"docker compose failed: {cout}\n{cerr}")

        if not skip_smoke:
            res = run_smoke(ssh, cfg)
            if not res.ok:
                env_rb = _remote_compose_env(usb)
                ssh.exec_text(f"{env_rb}; cd '{stack}' && docker compose down 2>/dev/null || true")
                if meta:
                    rollback(ssh, meta)
                raise RuntimeError("Smoke failed:\n" + "\n".join(res.messages))
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
        ssh.upload_file(out / "mihomo/mihomo-routing.sh", f"{stack}/mihomo/mihomo-routing.sh", mode=0o755)
        env = _remote_compose_env(usb)
        ssh.exec_text(f"{env}; cd '{stack}' && docker compose up -d 2>&1")
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
