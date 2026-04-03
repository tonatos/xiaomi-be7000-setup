from __future__ import annotations

import posixpath
import socket
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import paramiko


USB_DISCOVER_SCRIPT = r"""
UUID=$(uci -q get mi_docker.settings.device_uuid)
if [ -z "$UUID" ]; then echo "NO_UUID"; exit 0; fi
storage dump | grep -C3 "$UUID" | grep target: | awk '{print $2}'
"""


@dataclass
class RouterSSH:
    host: str
    password: str
    port: int = 22
    username: str = "root"
    _client: paramiko.SSHClient | None = None

    def connect(self) -> paramiko.SSHClient:
        if self._client is not None:
            return self._client
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(
            self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            allow_agent=False,
            look_for_keys=False,
        )
        self._client = c
        return c

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self) -> RouterSSH:
        self.connect()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def exec(self, cmd: str, timeout: int = 120) -> tuple[int, str, str]:
        client = self.connect()
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        stdin.close()
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        code = stdout.channel.recv_exit_status()
        return code, out, err

    def exec_text(self, cmd: str, timeout: int = 120) -> str:
        code, out, err = self.exec(cmd, timeout=timeout)
        if err.strip():
            out = f"{out}\n[stderr]\n{err}"
        if code != 0:
            out = f"{out}\n[exit {code}]"
        return out

    def usb_mount_from_router(self, override: str | None) -> str:
        if override and override.strip():
            return override.strip().rstrip("/")
        _, out, _ = self.exec(USB_DISCOVER_SCRIPT.strip())
        path = out.strip().splitlines()[-1] if out.strip() else ""
        if path == "NO_UUID" or not path:
            raise RuntimeError(
                "Не удалось определить USB: задайте usb.mount_path в router.yaml "
                "или привяжите USB в настройках Docker (mi_docker.settings.device_uuid)."
            )
        return path.rstrip("/")

    def upload_bytes(self, remote_path: str, data: bytes, mode: int = 0o644) -> None:
        client = self.connect()
        dirname = posixpath.dirname(remote_path)
        if dirname:
            self.exec_text(f"mkdir -p '{dirname}'")
        sftp = client.open_sftp()
        try:
            with sftp.open(remote_path, "wb") as rf:  # type: ignore[attr-defined]
                rf.write(data)
            sftp.chmod(remote_path, mode)
        finally:
            sftp.close()

    def upload_file(self, local: Path, remote_path: str, mode: int | None = None) -> None:
        data = local.read_bytes()
        m = mode if mode is not None else 0o644
        if local.suffix in {".sh"}:
            m = 0o755
        self.upload_bytes(remote_path, data, m)

    def upload_dir(self, local_root: Path, remote_root: str) -> None:
        local_root = local_root.resolve()
        for p in local_root.rglob("*"):
            if p.is_dir():
                continue
            rel = p.relative_to(local_root)
            rpath = PurePosixPath(remote_root) / rel.as_posix()
            self.upload_file(p, str(rpath).replace("\\", "/"))

    def download_bytes(self, remote_path: str) -> bytes:
        client = self.connect()
        sftp = client.open_sftp()
        try:
            sftp.stat(remote_path)
            with sftp.open(remote_path, "rb") as rf:  # type: ignore[attr-defined]
                return rf.read()
        except OSError as e:
            raise FileNotFoundError(remote_path) from e
        finally:
            sftp.close()

    def remote_path_exists(self, remote_path: str) -> bool:
        try:
            self.connect().open_sftp().stat(remote_path)
            return True
        except OSError:
            return False


def tcp_port_open(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_ssh_reachable(host: str, port: int, password: str, user: str = "root") -> bool:
    try:
        with RouterSSH(host=host, password=password, port=port, username=user) as r:
            code, out, _ = r.exec("echo ok")
            return code == 0 and "ok" in out
    except Exception:
        return False
