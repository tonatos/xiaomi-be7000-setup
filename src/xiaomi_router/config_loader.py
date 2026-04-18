from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import yaml

# Допустимый диапазон TCP/UDP-портов (IANA).
_MAX_PORT = 65535


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, Mapping):
            out[k] = _deep_merge(out[k], v)  # type: ignore[arg-type]
        else:
            out[k] = deepcopy(v) if isinstance(v, dict) else v
    return out


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def load_merged_config(
    main_path: Path,
    secrets_path: Path | None = None,
) -> dict[str, Any]:
    base_path = main_path.parent / "router.base.yaml"
    base_cfg = load_yaml(base_path) if base_path.exists() else {}
    user_cfg = load_yaml(main_path)
    cfg = _deep_merge(base_cfg, user_cfg)
    sec_path = secrets_path
    if sec_path is None:
        cand = main_path.parent / "router.secrets.yaml"
        if cand.exists():
            sec_path = cand
    secrets = load_yaml(sec_path) if sec_path and sec_path.exists() else {}

    if "ssh_password" in secrets:
        cfg.setdefault("router", {})["ssh_password"] = secrets.pop("ssh_password")
    cfg = _deep_merge(cfg, secrets)

    router = cfg.setdefault("router", {})
    if host := os.environ.get("ROUTER_HOST"):
        router["host"] = host
    if os.environ.get("ROUTER_SSH_PASSWORD"):
        router["ssh_password"] = os.environ["ROUTER_SSH_PASSWORD"]
    if port := os.environ.get("ROUTER_SSH_PORT"):
        router["ssh_port"] = int(port)
    if user := os.environ.get("ROUTER_SSH_USER"):
        router["ssh_user"] = user

    pub = cfg.setdefault("public_endpoint", {})
    if ph := os.environ.get("ROUTER_PUBLIC_HOST"):
        pub["host"] = ph

    return cfg


def validate_main_router_yaml_file(path: Path) -> None:
    """
    Проверяет, что пользовательский router.yaml существует и при парсинге даёт
    корневой YAML mapping (или пустой документ), а не список/скаляр.
    """
    if not path.exists():
        raise FileNotFoundError(str(path))
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if raw is not None and not isinstance(raw, dict):
        raise ValueError(
            f"{path}: в корне YAML ожидается объект (mapping), "
            f"получено {type(raw).__name__}"
        )


def _non_empty_str(v: Any, path: str) -> list[str]:
    errs: list[str] = []
    if not isinstance(v, str) or not v.strip():
        errs.append(f"{path}: ожидается непустая строка")
    return errs


def _port_errors(value: Any, path: str) -> list[str]:
    errs: list[str] = []
    try:
        p = int(value)
    except (TypeError, ValueError):
        errs.append(f"{path}: ожидается целое число (порт)")
        return errs
    if p < 1 or p > _MAX_PORT:
        errs.append(f"{path}: порт должен быть от 1 до {_MAX_PORT}, сейчас {p}")
    return errs


def validate_merged_config_for_deploy(cfg: dict[str, Any]) -> None:
    """
    Проверяет объединённый конфиг (base + router.yaml + secrets + env) перед deploy.
    Не заменяет require_router_password при SSH — дублирует проверку наличия пароля
    для раннего понятного сообщения.
    """
    errs: list[str] = []

    router = cfg.get("router")
    if not isinstance(router, dict):
        errs.append("router: ожидается объект с настройками SSH")
    else:
        errs.extend(_non_empty_str(router.get("host"), "router.host"))
        if "ssh_port" in router and router["ssh_port"] is not None:
            errs.extend(_port_errors(router["ssh_port"], "router.ssh_port"))
        su = router.get("ssh_user", "root")
        if su is not None and (not isinstance(su, str) or not str(su).strip()):
            errs.append("router.ssh_user: ожидается непустая строка")

    pwd = (cfg.get("router") or {}).get("ssh_password")
    if not (isinstance(pwd, str) and pwd.strip()) and not os.environ.get(
        "ROUTER_SSH_PASSWORD"
    ):
        errs.append(
            "Задайте router.ssh_password (или legacy secrets), либо ROUTER_SSH_PASSWORD"
        )

    services = cfg.get("services")
    if services is not None and not isinstance(services, dict):
        errs.append("services: ожидается объект")

    def check_enabled_service_ports(
        key: str,
        enabled: bool,
        port_specs: list[tuple[str, Any]],
    ) -> None:
        if not enabled:
            return
        svc = (services or {}).get(key) if isinstance(services, dict) else None
        if svc is not None and not isinstance(svc, dict):
            errs.append(f"services.{key}: ожидается объект")
            return
        for yaml_path, val in port_specs:
            if val is None:
                continue
            errs.extend(_port_errors(val, yaml_path))

    sv = services if isinstance(services, dict) else {}
    sx = sv.get("xray_server", {})
    x_en = bool(sx.get("enabled", True)) if isinstance(sx, dict) else True
    xi = cfg.get("xray", {}).get("inbound", {}) if isinstance(cfg.get("xray"), dict) else {}
    check_enabled_service_ports(
        "xray_server",
        x_en,
        [("xray.inbound.port", xi.get("port", 443) if isinstance(xi, dict) else 443)],
    )

    mh = sv.get("mihomo", {})
    m_en = bool(mh.get("enabled", True)) if isinstance(mh, dict) else True
    if isinstance(mh, dict):
        check_enabled_service_ports(
            "mihomo",
            m_en,
            [
                ("services.mihomo.socks_port", mh.get("socks_port", 7890)),
                ("services.mihomo.redir_port", mh.get("redir_port", 7891)),
            ],
        )

    if m_en:
        mihomo_cfg = cfg.get("mihomo")
        if not isinstance(mihomo_cfg, dict):
            errs.append(
                "mihomo: ожидается объект конфигурации при включённом services.mihomo"
            )
        elif "proxies" not in mihomo_cfg:
            errs.append(
                "mihomo: задайте секцию proxies (список outbound-прокси, см. router.example.yaml)"
            )
        else:
            proxies = mihomo_cfg["proxies"]
            if not isinstance(proxies, list):
                errs.append("mihomo.proxies: ожидается список (YAML sequence)")
            elif len(proxies) == 0:
                errs.append(
                    "mihomo.proxies: нужен хотя бы один прокси; пустой список недопустим"
                )
            else:
                for i, item in enumerate(proxies):
                    if not isinstance(item, dict):
                        errs.append(
                            f"mihomo.proxies[{i}]: каждый элемент должен быть объектом (mapping)"
                        )
                        continue
                    server = item.get("server")
                    if not isinstance(server, str) or not server.strip():
                        errs.append(
                            f"mihomo.proxies[{i}]: задайте непустой server (адрес upstream-прокси); "
                            "см. закомментированный пример в config/router.example.yaml"
                        )

    ts = sv.get("torrserver", {})
    t_en = bool(ts.get("enabled", True)) if isinstance(ts, dict) else True
    if isinstance(ts, dict):
        check_enabled_service_ports(
            "torrserver",
            t_en,
            [("services.torrserver.port", ts.get("port", 8090))],
        )

    md = sv.get("metacubexd", {})
    md_en = bool(md.get("enabled", True)) if isinstance(md, dict) else True
    if isinstance(md, dict):
        check_enabled_service_ports(
            "metacubexd",
            md_en,
            [("services.metacubexd.port", md.get("port", 9099))],
        )

    agh_svc = sv.get("adguardhome", {})
    ag_en = bool(agh_svc.get("enabled", True)) if isinstance(agh_svc, dict) else True
    _agh = cfg.get("adguardhome")
    agh_app = _agh if isinstance(_agh, dict) else {}
    if isinstance(agh_svc, dict):
        check_enabled_service_ports(
            "adguardhome",
            ag_en,
            [
                ("adguardhome.dns_port", agh_app.get("dns_port", 5353)),
                ("adguardhome.admin_port", agh_app.get("admin_port", 3000)),
            ],
        )

    if errs:
        raise ValueError("Конфиг не прошёл проверку:\n- " + "\n- ".join(errs))


def require_router_password(cfg: dict[str, Any]) -> str:
    pwd = cfg.get("router", {}).get("ssh_password")
    if not pwd:
        msg = (
            "Задайте пароль SSH: router.ssh_password в config/router.yaml "
            "(или legacy config/router.secrets.yaml), "
            "либо переменную окружения ROUTER_SSH_PASSWORD"
        )
        raise SystemExit(msg)
    return str(pwd)
