from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from xiaomi_router.paths import repo_root
from xiaomi_router.ssh_util import check_ssh_reachable


def xmir_root() -> Path:
    return repo_root() / "third_party" / "xmir-patcher"


def ensure_submodule_present() -> Path:
    root = xmir_root()
    if not (root / "connect.py").exists():
        raise SystemExit(
            f"Нет {root}/connect.py. Выполните: git submodule update --init third_party/xmir-patcher"
        )
    return root


def run_bootstrap_if_needed(
    host: str,
    *,
    ssh_port: int,
    ssh_password: str,
    ssh_user: str = "root",
    force: bool = False,
    web_password_hint: bool = True,
) -> None:
    if (
        not force
        and ssh_password
        and check_ssh_reachable(host, ssh_port, ssh_password, ssh_user)
    ):
        print("SSH уже доступен — bootstrap xmir-patcher пропущен.")
        return

    root = ensure_submodule_present()
    py = sys.executable
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)
    print("Запуск connect.py (эксплойт / доступ к устройству)…")
    if web_password_hint:
        print(
            "Если скрипт запросит пароль веб-интерфейса Xiaomi — введите его вручную "
            "(переменные окружения xmir-patcher см. в их README)."
        )
    r = subprocess.run(
        [py, "connect.py", host],
        cwd=root,
        env=env,
        check=False,
    )
    if r.returncode != 0:
        raise SystemExit(f"connect.py завершился с кодом {r.returncode}")

    print("Запуск install_ssh.py (постоянный SSH)…")
    r2 = subprocess.run(
        [py, "install_ssh.py"],
        cwd=root,
        env=env,
        check=False,
    )
    if r2.returncode != 0:
        raise SystemExit(f"install_ssh.py завершился с кодом {r2.returncode}")

    print("Готово. Проверьте вход по SSH и при необходимости задайте пароль root (passw.py в xmir-patcher).")
