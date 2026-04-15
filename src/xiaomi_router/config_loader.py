from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import yaml


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
