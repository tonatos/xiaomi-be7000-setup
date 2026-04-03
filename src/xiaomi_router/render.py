from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from xiaomi_router.paths import rendered_dir, repo_root, templates_dir


def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(templates_dir())),
        autoescape=select_autoescape(enabled_extensions=()),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def build_render_context(cfg: dict[str, Any], usb_mount: str) -> dict[str, Any]:
    stack_rel = cfg.get("stack", {}).get("relative_dir", "stack")
    stack_path = f"{usb_mount}/{stack_rel}".replace("//", "/")
    docker_bin = f"{usb_mount}/mi_docker/docker-binaries/docker"
    startup = cfg.get("startup", {})
    sm = cfg.get("services", {}).get("mihomo", {})
    return {
        **cfg,
        "usb_mount": usb_mount,
        "stack_path": stack_path,
        "docker_bin": docker_bin,
        "startup_base": startup.get("base_dir", "/data/startup"),
        "startup_autoruns": startup.get("autoruns_subdir", "autoruns"),
        "services_mihomo_redir_port": sm.get("redir_port", 7891),
        "services_mihomo_container_name": sm.get("container_name", "mihomo"),
    }


def render_all(cfg: dict[str, Any], usb_mount: str, out_dir: Path | None = None) -> Path:
    out = out_dir or rendered_dir()
    out.mkdir(parents=True, exist_ok=True)
    ctx = build_render_context(cfg, usb_mount)
    env = _jinja_env()

    def w(rel_out: str, content: str) -> None:
        p = out / rel_out
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    # xray
    tpl = env.get_template("xray/config.json.j2")
    xray_json = tpl.render(**ctx)
    json.loads(xray_json)  # validate
    w("configs/xray/config.json", xray_json)

    # mihomo
    tpl = env.get_template("mihomo/config.yaml.j2")
    w("configs/mihomo/config.yaml", tpl.render(**ctx))

    tpl = env.get_template("mihomo/mihomo-routing.sh.j2")
    w("mihomo/mihomo-routing.sh", tpl.render(**ctx))
    (out / "mihomo/mihomo-routing.sh").chmod(0o755)

    # compose
    tpl = env.get_template("compose/docker-compose.yml.j2")
    w("docker-compose.yml", tpl.render(**ctx))

    # autorun + startup
    tpl = env.get_template("startup.sh.j2")
    w("startup/startup.sh", tpl.render(**ctx))
    (out / "startup/startup.sh").chmod(0o755)

    tpl = env.get_template("autorun/010-start-docker.sh.j2")
    w("startup/autoruns/010-start-docker.sh", tpl.render(**ctx))
    (out / "startup/autoruns/010-start-docker.sh").chmod(0o755)

    tpl = env.get_template("autorun/020-mihomo-routing.sh.j2")
    w("startup/autoruns/020-mihomo-routing.sh", tpl.render(**ctx))
    (out / "startup/autoruns/020-mihomo-routing.sh").chmod(0o755)

    # marker for sync
    (out / ".xiaomi_router_revision").write_text(
        f"usb_mount={usb_mount}\nrepo={repo_root()}\n", encoding="utf-8"
    )

    return out


def render_local_preview(cfg: dict[str, Any], usb_placeholder: str = "/mnt/usb-PLACEHOLDER") -> Path:
    """Рендер без SSH (для проверки шаблонов)."""
    return render_all(cfg, usb_placeholder)
