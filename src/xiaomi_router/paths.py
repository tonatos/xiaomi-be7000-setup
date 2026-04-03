from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    """Корень репозитория (родитель каталога `src`)."""
    return Path(__file__).resolve().parents[2]


def templates_dir() -> Path:
    return repo_root() / "templates"


def default_config_path() -> Path:
    return repo_root() / "config" / "router.yaml"


def default_secrets_path() -> Path:
    return repo_root() / "config" / "router.secrets.yaml"


def rendered_dir() -> Path:
    p = repo_root() / "build" / "rendered"
    p.mkdir(parents=True, exist_ok=True)
    return p
