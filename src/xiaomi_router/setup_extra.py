from __future__ import annotations

import io
import posixpath
import ssl
import urllib.request
import zipfile
from typing import Any, Final

from xiaomi_router.config_loader import require_router_password
from xiaomi_router.ssh_util import RouterSSH

COMPOSE_VERSION: Final = "v5.1.1"
V2RAY_VERSION: Final = "v5.47.0"
ENTWARE_INSTALLER_URL: Final = "http://bin.entware.net/aarch64-k3.10/installer/generic.sh"


def _ssh_from_cfg(cfg: dict[str, Any]) -> RouterSSH:
    router = cfg["router"]
    return RouterSSH(
        host=str(router["host"]),
        password=require_router_password(cfg),
        port=int(router.get("ssh_port", 22)),
        username=str(router.get("ssh_user", "root")),
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
        usb_opt = f"{usb}/opt"
        usb_entware_root = f"{usb}/entware-opt"
        usb_env = f"{usb_opt}/usb-env.sh"
        runner = "/tmp/entware-setup.sh"
        script = f"""#!/bin/sh
set -e
USB_ENTWARE_ROOT='{usb_entware_root}'
USB_ENV='{usb_env}'
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
tmp=/tmp/entware-generic.sh
curl -fSL "$INSTALLER_URL" -o "$tmp"
sh "$tmp"
rm -f "$tmp"
/opt/bin/opkg update 2>&1 | head -40 || true
"""
        ssh.upload_bytes(runner, script.encode("utf-8"), mode=0o755)
        print(ssh.exec_text(f"'{runner}' 2>&1"))
    finally:
        ssh.close()


def _v2ray_zip_name(uname_m: str) -> str:
    if uname_m == "aarch64":
        return "v2ray-linux-arm64-v8a.zip"
    if uname_m == "armv7l":
        return "v2ray-linux-arm32-v7a.zip"
    if uname_m == "armv6l":
        return "v2ray-linux-arm32-v6.zip"
    raise ValueError(f"Неподдерживаемая архитектура: {uname_m}")


def _download_v2ray_zip(zip_name: str) -> bytes:
    url = f"https://github.com/v2fly/v2ray-core/releases/download/{V2RAY_VERSION}/{zip_name}"
    req = urllib.request.Request(url, headers={"User-Agent": "xiaomi-be7000-setup"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except ssl.SSLError:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
            return resp.read()


def setup_compose_and_optional_v2ray(
    cfg: dict[str, Any],
    *,
    compose: bool = True,
    v2ray: bool = False,
    write_profile: bool = False,
) -> None:
    ssh = _ssh_from_cfg(cfg)
    try:
        usb = ssh.usb_mount_from_router(cfg.get("usb", {}).get("mount_path"))
        _, uname, _ = ssh.exec("uname -m")
        uname_m = uname.strip().splitlines()[-1]

        if compose:
            compose_usb_hook = (
                f"if [ -f '{usb}/opt/docker-cli/compose-env.sh' ]; then "
                f". '{usb}/opt/docker-cli/compose-env.sh'; fi"
            )
            remote = rf"""
set -e
UUID=$(uci -q get mi_docker.settings.device_uuid)
STORAGE_DIR=$(storage dump | grep -C3 "$UUID" | grep target: | awk '{{print $2}}')
OPT="$STORAGE_DIR/opt"
mkdir -p "$OPT"
U=$(uname -m)
case "$U" in
  aarch64) COMPOSE_ASSET=docker-compose-linux-aarch64 ;;
  armv7l) COMPOSE_ASSET=docker-compose-linux-armv7 ;;
  armv6l) COMPOSE_ASSET=docker-compose-linux-armv6 ;;
  *) echo "ERROR arch $U"; exit 1 ;;
esac
DOCKER_CONFIG="$OPT/docker-cli"
mkdir -p "$DOCKER_CONFIG/cli-plugins"
URL="https://github.com/docker/compose/releases/download/{COMPOSE_VERSION}/$COMPOSE_ASSET"
curl -fSL "$URL" -o "$DOCKER_CONFIG/cli-plugins/docker-compose"
chmod +x "$DOCKER_CONFIG/cli-plugins/docker-compose"
ENV_SH="$DOCKER_CONFIG/compose-env.sh"
cat > "$ENV_SH" << ENVEOF
export DOCKER_CONFIG="$DOCKER_CONFIG"
export PATH="$STORAGE_DIR/mi_docker/docker-binaries:$PATH"
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
            print(ssh.exec_text(remote))
            if write_profile:
                env_sh = f"{usb}/opt/docker-cli/compose-env.sh"
                ssh.exec_text(
                    f"grep -qF 'docker-cli/compose-env.sh' /etc/profile 2>/dev/null || "
                    f"echo \". '{env_sh}'\" >> /etc/profile"
                )

        if v2ray:
            zip_name = _v2ray_zip_name(uname_m)
            raw = _download_v2ray_zip(zip_name)
            remote_dir = f"{usb}/opt/v2ray"
            ssh.exec_text(f"mkdir -p '{remote_dir}'")
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename
                    if name.startswith("/") or ".." in name.split("/"):
                        continue
                    remote_path = f"{remote_dir}/{name}"
                    parent = posixpath.dirname(remote_path)
                    if parent != remote_dir:
                        ssh.exec_text(f"mkdir -p '{parent}'")
                    ssh.upload_bytes(remote_path, zf.read(info))
            ssh.exec_text(f"chmod +x '{remote_dir}/v2ray' 2>/dev/null || true")
            print(ssh.exec_text(f"'{remote_dir}/v2ray' version"))
    finally:
        ssh.close()
