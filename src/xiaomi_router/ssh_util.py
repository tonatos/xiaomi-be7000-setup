from __future__ import annotations

import posixpath
import socket
from collections.abc import Callable
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
            # Dropbear на роутере предлагает только ssh-rsa (SHA1).
            # Paramiko 2.9+ отключает его по умолчанию — явно разрешаем.
            disabled_algorithms={"pubkeys": ["rsa-sha2-256", "rsa-sha2-512"]},
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

    def exec_streaming(
        self,
        cmd: str,
        log: Callable[[str], None],
        timeout: int = 300,
    ) -> int:
        """Выполняет команду и построчно передаёт stdout/stderr в log в реальном времени."""
        client = self.connect()
        transport = client.get_transport()
        assert transport is not None
        chan = transport.open_session()
        chan.settimeout(timeout)
        chan.exec_command(cmd)
        chan.shutdown_write()
        buf = b""
        while True:
            if chan.recv_ready():
                chunk = chan.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    log(line.decode(errors="replace"))
            elif chan.recv_stderr_ready():
                chunk = chan.recv_stderr(4096)
                if chunk:
                    buf += chunk
            elif chan.exit_status_ready():
                break
            else:
                import time
                time.sleep(0.05)
        # Flush remaining buffer
        if buf:
            log(buf.decode(errors="replace"))
        return chan.recv_exit_status()

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
        # Не используем SFTP — Dropbear на роутере не поддерживает sftp-subsystem.
        # Данные передаём через stdin exec-канала (cat > file).
        dirname = posixpath.dirname(remote_path)
        if dirname:
            self.exec_text(f"mkdir -p '{dirname}'")
        oct_mode = oct(mode)[2:]
        client = self.connect()
        stdin, stdout, stderr = client.exec_command(
            f"cat > '{remote_path}' && chmod {oct_mode} '{remote_path}'",
            timeout=60,
        )
        stdin.write(data)
        stdin.channel.shutdown_write()
        exit_code = stdout.channel.recv_exit_status()
        if exit_code != 0:
            err = stderr.read().decode(errors="replace")
            raise RuntimeError(f"upload_bytes failed [{remote_path}]: {err}")

    def upload_file(self, local: Path, remote_path: str, mode: int | None = None) -> None:
        data = local.read_bytes()
        m = mode if mode is not None else 0o644
        if local.suffix in {".sh"}:
            m = 0o755
        # На Windows рендер создаёт файлы с CRLF (\r\n).
        # BusyBox sh на роутере не обрабатывает \r — нормализуем для всех текстовых форматов.
        if local.suffix in {".sh", ".yaml", ".json", ".conf", ".txt"}:
            data = data.replace(b"\r\n", b"\n")
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
        stdin, stdout, stderr = client.exec_command(
            f"cat '{remote_path}'", timeout=60
        )
        stdin.close()
        data = stdout.read()
        exit_code = stdout.channel.recv_exit_status()
        if exit_code != 0:
            raise FileNotFoundError(remote_path)
        return data  # type: ignore[return-value]

    def remote_path_exists(self, remote_path: str) -> bool:
        code, _, _ = self.exec(f"test -e '{remote_path}'")
        return code == 0


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
