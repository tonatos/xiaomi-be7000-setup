from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from xiaomi_router.config_loader import require_router_password
from xiaomi_router.ssh_util import RouterSSH

COMPOSE_VERSION: Final = "v5.1.1"
ENTWARE_INSTALLER_URL: Final = "http://bin.entware.net/aarch64-k3.10/installer/generic.sh"
_Noop: Callable[[str], None] = lambda _: None


def _ssh_from_cfg(cfg: dict[str, Any]) -> RouterSSH:
    router = cfg["router"]
    return RouterSSH(
        host=str(router["host"]),
        password=require_router_password(cfg),
        port=int(router.get("ssh_port", 22)),
        username=str(router.get("ssh_user", "root")),
    )


def _remote_compose_env(usb: str) -> str:
    return (
        f"export PATH='{usb}/mi_docker/docker-binaries:'\"$PATH\"; "
        f"if [ -f '{usb}/opt/usb-env.sh' ]; then . '{usb}/opt/usb-env.sh'; fi; "
        f"if [ -f '{usb}/opt/docker-cli/compose-env.sh' ]; then "
        f". '{usb}/opt/docker-cli/compose-env.sh'; fi"
    )


def _raise_if_failed(code: int, out: str, err: str, *, step: str) -> None:
    if code == 0:
        return
    details = out.strip()
    if err.strip():
        details = f"{details}\n[stderr]\n{err.strip()}".strip()
    raise RuntimeError(f"{step} завершился с ошибкой (exit={code}).\n{details}")


def has_docker_compose(ssh: RouterSSH, usb: str) -> bool:
    code, _, _ = ssh.exec(
        f"{_remote_compose_env(usb)}; docker compose version >/dev/null 2>&1",
        timeout=30,
    )
    return code == 0


def install_entware_on_usb(
    ssh: RouterSSH,
    usb: str,
    *,
    log: Callable[[str], None] = _Noop,
) -> None:
    usb_entware_root = f"{usb}/entware-opt"
    script = f"""#!/bin/sh
set -e
USB_ENTWARE_ROOT='{usb_entware_root}'
INSTALLER_URL='{ENTWARE_INSTALLER_URL}'

mkdir -p "$USB_ENTWARE_ROOT"
if [ -d /opt/filetunnel ] && [ ! -d "$USB_ENTWARE_ROOT/filetunnel" ]; then
  cp -a /opt/filetunnel "$USB_ENTWARE_ROOT/" 2>/dev/null || true
fi

if mount | grep -q ' /opt '; then
  echo 'Already mounted /opt'
else
  mount --bind "$USB_ENTWARE_ROOT" /opt
fi

if [ -x /opt/bin/opkg ]; then
  echo 'Entware already installed'
else
  tmp=/tmp/entware-generic.sh
  if command -v curl >/dev/null 2>&1; then
    curl -fSL "$INSTALLER_URL" -o "$tmp"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$tmp" "$INSTALLER_URL"
  else
    echo 'ERROR: neither curl nor wget is available for Entware installer'
    exit 42
  fi
  sh "$tmp"
  rm -f "$tmp"
fi

/opt/bin/opkg update 2>&1 | head -40 || true
"""
    log("      Устанавливаю Entware на USB (при необходимости)...")
    code, out, err = ssh.exec(script, timeout=300)
    _raise_if_failed(code, out, err, step="setup-entware")
    for line in out.strip().splitlines():
        log(f"      {line}")


def install_compose_plugin(
    ssh: RouterSSH,
    usb: str,
    *,
    write_profile: bool = False,
    log: Callable[[str], None] = _Noop,
) -> None:
    compose_usb_hook = (
        f"if [ -f '{usb}/opt/docker-cli/compose-env.sh' ]; then "
        f". '{usb}/opt/docker-cli/compose-env.sh'; fi"
    )
    script = rf"""#!/bin/sh
set -e
USB='{usb}'
OPT="$USB/opt"
DOCKER_BIN="$USB/mi_docker/docker-binaries"

if [ ! -x "$DOCKER_BIN/docker" ]; then
  echo "ERROR: docker binary not found at $DOCKER_BIN/docker"
  exit 41
fi

mkdir -p "$OPT"
U=$(uname -m)
case "$U" in
  aarch64) COMPOSE_ASSET=docker-compose-linux-aarch64 ;;
  armv7l) COMPOSE_ASSET=docker-compose-linux-armv7 ;;
  armv6l) COMPOSE_ASSET=docker-compose-linux-armv6 ;;
  *) echo "ERROR arch $U"; exit 1 ;;
esac

DOCKER_CONFIG="$OPT/docker-cli"
TARGET="$DOCKER_CONFIG/cli-plugins/docker-compose"
mkdir -p "$DOCKER_CONFIG/cli-plugins"
URL="https://github.com/docker/compose/releases/download/{COMPOSE_VERSION}/$COMPOSE_ASSET"

if command -v curl >/dev/null 2>&1; then
  curl -fSL "$URL" -o "$TARGET"
elif command -v wget >/dev/null 2>&1; then
  wget -O "$TARGET" "$URL"
else
  echo "ERROR: neither curl nor wget is available"
  exit 42
fi

chmod +x "$TARGET"

ENV_SH="$DOCKER_CONFIG/compose-env.sh"
cat > "$ENV_SH" << ENVEOF
export DOCKER_CONFIG="$DOCKER_CONFIG"
export PATH="$DOCKER_BIN:\$PATH"
ENVEOF
chmod +x "$ENV_SH"

USB_ENV="$OPT/usb-env.sh"
if [ -f "$USB_ENV" ] && ! grep -qF "compose-env.sh" "$USB_ENV" 2>/dev/null; then
  echo "" >> "$USB_ENV"
  echo "{compose_usb_hook}" >> "$USB_ENV"
fi

. "$ENV_SH"
docker compose version
"""
    log("      Устанавливаю docker compose plugin на USB...")
    code, out, err = ssh.exec(script, timeout=300)
    _raise_if_failed(code, out, err, step="setup-compose")
    for line in out.strip().splitlines():
        log(f"      {line}")

    if write_profile:
        env_sh = f"{usb}/opt/docker-cli/compose-env.sh"
        code, out, err = ssh.exec(
            f"grep -qF 'docker-cli/compose-env.sh' /etc/profile 2>/dev/null || "
            f"echo \". '{env_sh}'\" >> /etc/profile"
        )
        _raise_if_failed(code, out, err, step="write /etc/profile compose env")


def ensure_compose_with_optional_entware(
    ssh: RouterSSH,
    usb: str,
    *,
    log: Callable[[str], None] = _Noop,
) -> None:
    if has_docker_compose(ssh, usb):
        log("      docker compose уже доступен.")
        return

    log("      docker compose не найден — запускаю auto-setup.")
    try:
        install_compose_plugin(ssh, usb, log=log)
    except RuntimeError:
        log("      setup-compose не удался, пробую setup-entware и повтор.")
        install_entware_on_usb(ssh, usb, log=log)
        install_compose_plugin(ssh, usb, log=log)

    if not has_docker_compose(ssh, usb):
        raise RuntimeError(
            "docker compose по-прежнему недоступен после setup-compose/setup-entware"
        )


def setup_opkg_usb(cfg: dict[str, Any]) -> None:
    ssh = _ssh_from_cfg(cfg)
    try:
        usb = ssh.usb_mount_from_router(cfg.get("usb", {}).get("mount_path"))
        opt = f"{usb}/opt"
        opkg_root = f"{opt}/opkg"
        lists = f"{opkg_root}/lists"
        tmp = f"{opkg_root}/tmp"
        cache = f"{opkg_root}/cache"
        repo = f"{opkg_root}/repo"
        conf = f"{opkg_root}/opkg-usb.conf"
        wrapper = f"{opt}/bin/opkg-usb"
        env_sh = f"{opkg_root}/opkg-usb.env.sh"
        usb_env = f"{opt}/usb-env.sh"
        readme = f"{opkg_root}/README-opkg-usb.txt"

        ssh.exec_text(
            f"mkdir -p '{opt}/bin' '{lists}' '{tmp}' '{cache}' '{repo}'"
        )

        conf_text = "\n".join(
            [
                "dest root /",
                f"dest usb {opt}",
                f"lists_dir ext {lists}",
                f"option tmp_dir {tmp}",
                f"option cache {cache}",
                "",
                f"src/gz local file:{repo}",
                "",
            ]
        )
        ssh.upload_bytes(conf, conf_text.encode("utf-8"))

        wrapper_text = "\n".join(
            [
                "#!/bin/sh",
                f"exec /bin/opkg -f '{conf}' -d usb \"$@\"",
                "",
            ]
        )
        ssh.upload_bytes(wrapper, wrapper_text.encode("utf-8"), mode=0o755)

        usb_env_text = "\n".join(
            [
                "#!/bin/sh",
                f"if [ -d '{usb}/entware-opt' ]; then",
                "  if [ ! -x /opt/bin/opkg ]; then",
                "    if ! mount | grep -q ' /opt '; then",
                f"      mount --bind '{usb}/entware-opt' /opt 2>/dev/null || true",
                "    fi",
                "  fi",
                "  if [ -x /opt/bin/opkg ]; then",
                "    export PATH='/opt/bin:/opt/sbin:'\"$PATH\"",
                "    export LD_LIBRARY_PATH='/opt/lib:'\"${LD_LIBRARY_PATH:-}\"",
                "  fi",
                "fi",
                f"export PATH='{opt}/bin:'\"$PATH\"",
                f"export PATH='{usb}/mi_docker/docker-binaries:'\"$PATH\"",
                f"if [ -f '{opt}/docker-cli/compose-env.sh' ]; then",
                f"  . '{opt}/docker-cli/compose-env.sh'",
                "fi",
                "",
            ]
        )
        ssh.upload_bytes(usb_env, usb_env_text.encode("utf-8"), mode=0o755)

        ssh.exec_text(
            f"sh -c \"cd '{repo}' && : > Packages && gzip -f -k Packages\""
        )

        readme_text = (
            "opkg-usb: см. документацию проекта xiaomi-be7000-setup (README).\n"
            f"wrapper: {wrapper}\nusb-env: {usb_env}\n"
        )
        ssh.upload_bytes(readme, readme_text.encode("utf-8"))
        print(f"opkg-usb: {wrapper}")
    finally:
        ssh.close()


def setup_entware(cfg: dict[str, Any]) -> None:
    ssh = _ssh_from_cfg(cfg)
    try:
        usb = ssh.usb_mount_from_router(cfg.get("usb", {}).get("mount_path"))
        install_entware_on_usb(ssh, usb, log=print)
    finally:
        ssh.close()


def setup_compose(
    cfg: dict[str, Any],
    *,
    write_profile: bool = False,
) -> None:
    ssh = _ssh_from_cfg(cfg)
    try:
        usb = ssh.usb_mount_from_router(cfg.get("usb", {}).get("mount_path"))
        install_compose_plugin(ssh, usb, write_profile=write_profile, log=print)
    finally:
        ssh.close()
