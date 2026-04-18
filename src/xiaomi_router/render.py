from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from xiaomi_router.paths import rendered_dir, repo_root, templates_dir


class _IndentedDumper(yaml.Dumper):
    """Dumper с отступом для элементов последовательностей внутри словарей."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:  # type: ignore[override]
        return super().increase_indent(flow=flow, indentless=False)


def _toyaml(value: Any) -> str:
    """Сериализует Python-значение в YAML-строку без завершающего переноса."""
    return yaml.dump(
        value,
        Dumper=_IndentedDumper,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    ).rstrip("\n")


def _jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(templates_dir())),
        autoescape=select_autoescape(enabled_extensions=()),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["toyaml"] = _toyaml
    return env


def _extract_mihomo_controller_port(cfg: dict[str, Any]) -> int:
    controller = str(cfg.get("mihomo", {}).get("external-controller", "0.0.0.0:9090")).strip()
    if ":" not in controller:
        return 9090
    port_part = controller.rsplit(":", maxsplit=1)[-1].strip()
    try:
        return int(port_part)
    except ValueError:
        return 9090


def _as_flat_str_list(value: Any, default: list[str]) -> list[str]:
    """
    Нормализует YAML-значение в список строк.

    Важно для user-config: при ошибках копипасты в YAML могут появляться вложенные
    списки, что потом ломает сторонние парсеры (например AGH с ошибкой
    "cannot unmarshal !!seq into string").
    """

    def is_seq(v: Any) -> bool:
        return isinstance(v, (list, tuple))

    out: list[str] = []
    stack: list[Any] = [value]
    while stack:
        cur = stack.pop(0)
        if cur is None:
            continue
        if isinstance(cur, str):
            s = cur.strip()
            if s:
                out.append(s)
            continue
        if is_seq(cur):
            stack[0:0] = list(cur)
            continue
        s = str(cur).strip()
        if s:
            out.append(s)

    return out or default


def _validate_rendered_text(rel_out: str, content: str) -> None:
    """Проверка синтаксиса JSON/YAML после Jinja (до записи на диск)."""
    suf = Path(rel_out).suffix.lower()
    if suf == ".json":
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            raise ValueError(f"После рендера {rel_out}: невалидный JSON ({e})") from e
        return
    if suf in (".yaml", ".yml"):
        try:
            list(yaml.safe_load_all(content))
        except yaml.YAMLError as e:
            raise ValueError(f"После рендера {rel_out}: невалидный YAML ({e})") from e


def build_render_context(cfg: dict[str, Any], usb_mount: str) -> dict[str, Any]:
    stack_rel = cfg.get("stack", {}).get("relative_dir", "stack")
    stack_path = f"{usb_mount}/{stack_rel}".replace("//", "/")
    docker_bin = f"{usb_mount}/mi_docker/docker-binaries/docker"
    startup = cfg.get("startup", {})
    sm = cfg.get("services", {}).get("mihomo", {})
    sd = cfg.get("services", {}).get("metacubexd", {})
    _agh = cfg.get("adguardhome")
    agh_app = _agh if isinstance(_agh, dict) else {}
    mihomo_enabled = bool(sm.get("enabled", True))
    agh_http_proxy = str(agh_app.get("http_proxy", "") or "").strip()
    if agh_http_proxy and not mihomo_enabled:
        if not bool(agh_app.get("http_proxy_ignore_mihomo_state", False)):
            agh_http_proxy = ""
    router_host = str(cfg.get("router", {}).get("host", "192.168.31.1")).strip() or "192.168.31.1"
    mihomo_controller_port = _extract_mihomo_controller_port(cfg)
    return {
        **cfg,
        "usb_mount": usb_mount,
        "stack_path": stack_path,
        "docker_bin": docker_bin,
        "startup_base": startup.get("base_dir", "/data/startup"),
        "startup_autoruns": startup.get("autoruns_subdir", "autoruns"),
        "services_mihomo_redir_port": sm.get("redir_port", 7891),
        "services_mihomo_container_name": sm.get("container_name", "mihomo"),
        "services_metacubexd_port": sd.get("port", 9099),
        "services_metacubexd_default_backend_url": sd.get(
            "default_backend_url",
            f"http://{router_host}:{mihomo_controller_port}",
        ),
        "adguardhome_dns_port": agh_app.get("dns_port", 5353),
        "adguardhome_admin_port": agh_app.get("admin_port", 3000),
        "adguardhome_upstream_dns": _as_flat_str_list(
            agh_app.get("upstream_dns"),
            default=["https://9.9.9.10/dns-query", "https://149.112.112.10/dns-query"],
        ),
        "adguardhome_bootstrap_dns": _as_flat_str_list(
            agh_app.get("bootstrap_dns"),
            default=[],
        ),
        "adguardhome_fallback_dns": _as_flat_str_list(
            agh_app.get("fallback_dns"),
            default=[],
        ),
        "adguardhome_filters_update_interval": agh_app.get("filters_update_interval", 24),
        "adguardhome_bind_hosts": _as_flat_str_list(
            agh_app.get("bind_hosts"),
            default=["0.0.0.0"],
        ),
        "adguardhome_http_proxy": agh_http_proxy,
        "adguardhome_allowed_clients": _as_flat_str_list(
            agh_app.get("allowed_clients"),
            default=[],
        ),
        "adguardhome_disallowed_clients": _as_flat_str_list(
            agh_app.get("disallowed_clients"),
            default=[],
        ),
    }


def render_all(cfg: dict[str, Any], usb_mount: str, out_dir: Path | None = None) -> Path:
    out = out_dir or rendered_dir()
    out.mkdir(parents=True, exist_ok=True)
    ctx = build_render_context(cfg, usb_mount)
    env = _jinja_env()

    def w(rel_out: str, content: str) -> None:
        _validate_rendered_text(rel_out, content)
        p = out / rel_out
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    # xray
    tpl = env.get_template("xray/config.json.j2")
    w("configs/xray/config.json", tpl.render(**ctx))

    # mihomo
    tpl = env.get_template("mihomo/config.yaml.j2")
    w("configs/mihomo/config.yaml", tpl.render(**ctx))

    tpl = env.get_template("mihomo/mihomo-routing.sh.j2")
    w("mihomo/mihomo-routing.sh", tpl.render(**ctx))
    (out / "mihomo/mihomo-routing.sh").chmod(0o755)

    tpl = env.get_template("mihomo/rollback.sh.j2")
    w("mihomo/rollback.sh", tpl.render(**ctx))
    (out / "mihomo/rollback.sh").chmod(0o755)

    # compose
    tpl = env.get_template("compose/docker-compose.yml.j2")
    w("docker-compose.yml", tpl.render(**ctx))

    # adguardhome
    tpl = env.get_template("adguardhome/AdGuardHome.yaml.j2")
    w("configs/adguardhome/conf/AdGuardHome.yaml", tpl.render(**ctx))

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
