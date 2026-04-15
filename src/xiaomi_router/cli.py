from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from xiaomi_router.config_loader import load_merged_config, require_router_password
from xiaomi_router.diagnose import run_diagnose
from xiaomi_router.paths import default_config_path, default_secrets_path
from xiaomi_router.pipeline import (
    cmd_rollback,
    deploy,
    pull_configs,
    push_rendered_only,
)
from xiaomi_router.render import render_all, render_local_preview
from xiaomi_router.setup_extra import (
    setup_compose,
    setup_entware,
    setup_opkg_usb,
)
from xiaomi_router.smoke import run_smoke
from xiaomi_router.ssh_util import RouterSSH
from xiaomi_router.vless_link import build_vless_reality_link
from xiaomi_router.xmir_bootstrap import run_bootstrap_if_needed

app = typer.Typer(no_args_is_help=True, help="Конфигуратор стека для Xiaomi BE7000.")


def _load(
    config: Optional[Path],
    secrets: Optional[Path],
) -> dict:
    main_p = config or default_config_path()
    if not main_p.exists():
        ex = main_p.parent / "router.example.yaml"
        typer.echo(
            typer.style(
                f"Нет {main_p}. Скопируйте {ex} в {main_p} и заполните.",
                fg=typer.colors.RED,
            ),
            err=True,
        )
        raise typer.Exit(1)
    sec_p = secrets
    if sec_p is None and default_secrets_path().exists():
        sec_p = default_secrets_path()
    return load_merged_config(main_p, sec_p)


@app.command("render")
def cmd_render(
    usb_placeholder: str = typer.Option(
        "/mnt/usb-PLACEHOLDER",
        "--usb-placeholder",
        help="Для локального рендера без SSH",
    ),
    discover: bool = typer.Option(
        False,
        "--discover-usb",
        help="Определить USB по SSH и отрендерить в build/rendered",
    ),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    secrets: Optional[Path] = typer.Option(None, "--secrets", "-s"),
) -> None:
    """Сгенерировать файлы в build/rendered."""
    cfg = _load(config, secrets)
    if discover:
        pwd = require_router_password(cfg)
        r = cfg["router"]
        ssh = RouterSSH(
            host=str(r["host"]),
            password=pwd,
            port=int(r.get("ssh_port", 22)),
            username=str(r.get("ssh_user", "root")),
        )
        try:
            usb = ssh.usb_mount_from_router(cfg.get("usb", {}).get("mount_path"))
            out = render_all(cfg, usb)
        finally:
            ssh.close()
        typer.echo(out)
    else:
        out = render_local_preview(cfg, usb_placeholder)
        typer.echo(out)


@app.command("deploy")
def cmd_deploy(
    skip_smoke: bool = typer.Option(False, "--skip-smoke"),
    skip_backup: bool = typer.Option(False, "--skip-backup"),
    no_rollback_on_smoke_fail: bool = typer.Option(
        False,
        "--no-rollback-on-smoke-fail",
        help="Не откатывать изменения при провале smoke",
    ),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    secrets: Optional[Path] = typer.Option(None, "--secrets", "-s"),
) -> None:
    """Бэкап, рендер, загрузка, UCI, docker compose up, smoke."""
    cfg = _load(config, secrets)
    try:
        meta = deploy(
            cfg,
            skip_smoke=skip_smoke,
            skip_backup=skip_backup,
            rollback_on_smoke_fail=not no_rollback_on_smoke_fail,
            log=typer.echo,
        )
        typer.echo(typer.style("✓ Deploy OK.", fg=typer.colors.GREEN))
        if meta:
            typer.echo(f"  Бэкап: {meta.get('tar_startup')}")
    except Exception as e:
        typer.echo(typer.style(f"✗ {e}", fg=typer.colors.RED), err=True)
        raise typer.Exit(1) from e


@app.command("smoke")
def cmd_smoke(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    secrets: Optional[Path] = typer.Option(None, "--secrets", "-s"),
) -> None:
    cfg = _load(config, secrets)
    pwd = require_router_password(cfg)
    r = cfg["router"]
    ssh = RouterSSH(
        host=str(r["host"]),
        password=pwd,
        port=int(r.get("ssh_port", 22)),
        username=str(r.get("ssh_user", "root")),
    )
    try:
        res = run_smoke(ssh, cfg, log=typer.echo)
        typer.echo("\n".join(res.messages))
        if not res.ok:
            raise typer.Exit(1)
    finally:
        ssh.close()


@app.command("rollback")
def cmd_rollback_cli(
    meta_json: Path = typer.Argument(..., exists=True, readable=True),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    secrets: Optional[Path] = typer.Option(None, "--secrets", "-s"),
) -> None:
    cfg = _load(config, secrets)
    cmd_rollback(cfg, meta_json)
    typer.echo("Rollback выполнен (см. логи на роутере при необходимости).")


@app.command("sync-pull")
def cmd_sync_pull(
    dest: Path = typer.Option(
        Path("build/synced-from-router"),
        "--dest",
        "-d",
        help="Локальный каталог",
    ),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    secrets: Optional[Path] = typer.Option(None, "--secrets", "-s"),
) -> None:
    cfg = _load(config, secrets)
    pull_configs(cfg, dest)
    typer.echo(dest)


@app.command("sync-push")
def cmd_sync_push(
    rendered: Optional[Path] = typer.Option(
        None,
        "--from",
        help="Каталог с render (по умолчанию build/rendered)",
    ),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    secrets: Optional[Path] = typer.Option(None, "--secrets", "-s"),
) -> None:
    cfg = _load(config, secrets)
    push_rendered_only(cfg, rendered)
    typer.echo("sync-push OK.")


@app.command("vless-link")
def cmd_vless_link(
    host: Optional[str] = typer.Option(
        None,
        "--host",
        help="Публичный IP/домен (если не задан в public_endpoint.host)",
    ),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    secrets: Optional[Path] = typer.Option(None, "--secrets", "-s"),
) -> None:
    cfg = _load(config, secrets)
    link = build_vless_reality_link(cfg, override_host=host)
    typer.echo(link)
    typer.echo(
        "\nУбедитесь, что на WAN проброшен порт inbound (обычно 443) и у провайдера "
        "есть «белый» IP, если клиенты подключаются из интернета."
    )


@app.command("bootstrap-ssh")
def cmd_bootstrap_ssh(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    secrets: Optional[Path] = typer.Option(None, "--secrets", "-s"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Запустить xmir-patcher даже если SSH уже отвечает",
    ),
) -> None:
    """Если порт 22 закрыт или --force: connect.py + install_ssh.py из third_party/xmir-patcher."""
    from xiaomi_router.ssh_util import check_ssh_reachable, tcp_port_open

    cfg = _load(config, secrets)
    r = cfg["router"]
    host = str(r["host"])
    port = int(r.get("ssh_port", 22))
    user = str(r.get("ssh_user", "root"))
    pwd = (r.get("ssh_password") or "").strip()

    if not force:
        if tcp_port_open(host, port):
            if pwd and check_ssh_reachable(host, port, pwd, user):
                typer.echo("SSH уже доступен. Используйте --force для повторного патча.")
                return
            if pwd and not check_ssh_reachable(host, port, pwd, user):
                typer.echo(
                    typer.style(
                        "Порт 22 открыт, но пароль не подошёл. "
                        "Исправьте router.ssh_password / ROUTER_SSH_PASSWORD "
                        "или используйте --force.",
                        fg=typer.colors.RED,
                    ),
                    err=True,
                )
                raise typer.Exit(1)
            typer.echo(
                "Порт 22 открыт, пароль не задан — считаем SSH доступным. "
                "Для принудительного патча: --force"
            )
            return

    run_bootstrap_if_needed(
        host, ssh_port=port, ssh_password=pwd, ssh_user=user, force=force
    )


@app.command("diagnose")
def cmd_diagnose(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    secrets: Optional[Path] = typer.Option(None, "--secrets", "-s"),
) -> None:
    cfg = _load(config, secrets)
    run_diagnose(cfg)


@app.command("setup-opkg-usb")
def cmd_setup_opkg(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    secrets: Optional[Path] = typer.Option(None, "--secrets", "-s"),
) -> None:
    cfg = _load(config, secrets)
    setup_opkg_usb(cfg)


@app.command("setup-entware")
def cmd_setup_entware(
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    secrets: Optional[Path] = typer.Option(None, "--secrets", "-s"),
) -> None:
    cfg = _load(config, secrets)
    setup_entware(cfg)


@app.command("setup-compose")
def cmd_setup_compose(
    write_profile: bool = typer.Option(
        False,
        "--write-profile",
        help="Добавить compose env в /etc/profile на роутере",
    ),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
    secrets: Optional[Path] = typer.Option(None, "--secrets", "-s"),
) -> None:
    cfg = _load(config, secrets)
    setup_compose(cfg, write_profile=write_profile)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
