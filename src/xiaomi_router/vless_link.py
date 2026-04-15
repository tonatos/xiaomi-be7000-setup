from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote, urlencode

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519


def reality_public_key_b64url(private_key_b64url: str) -> str:
    """Публичный ключ Reality (URL-safe base64) из приватного, как в Xray."""
    s = private_key_b64url.strip()
    pad = s + "=" * ((4 - len(s) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(pad)
    except Exception as e:
        raise ValueError("privateKey не похож на base64url") from e
    if len(raw) < 32:
        raise ValueError("privateKey после декодирования короче 32 байт")
    priv = x25519.X25519PrivateKey.from_private_bytes(raw[:32])
    pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return base64.urlsafe_b64encode(pub).decode("ascii").rstrip("=")


def build_vless_reality_link(
    cfg: dict[str, Any],
    *,
    override_host: str | None = None,
) -> str:
    x = cfg.get("xray", {})
    pub = cfg.get("public_endpoint", {})
    host = (override_host or pub.get("host") or "").strip()
    if not host:
        raise SystemExit(
            "Укажите public_endpoint.host в router.yaml или передайте --host для vless-ссылки."
        )

    inbound = x.get("inbound", {})
    port = int(inbound.get("port", 443))
    clients = x.get("clients") or []
    first_client = clients[0] if isinstance(clients, list) and clients else {}
    uuid = x.get("vless_uuid") or first_client.get("id")
    if not uuid:
        raise SystemExit("Нет xray.clients[0].id (или legacy xray.vless_uuid) в router.yaml")

    reality = x.get("reality", {})
    sni = str(reality.get("server_names", ["ya.ru"])[0])
    fp = str(reality.get("fingerprint", "chrome"))
    priv = x.get("reality_private_key") or reality.get("private_key")
    if not priv:
        raise SystemExit(
            "Нет xray.reality.private_key (или legacy xray.reality_private_key) в router.yaml"
        )
    try:
        pbk = reality_public_key_b64url(str(priv))
    except ValueError as e:
        raise SystemExit(
            "Некорректный ключ Reality "
            "(xray.reality.private_key / xray.reality_private_key): "
            f"{e}"
        ) from e
    short_ids = x.get("short_ids") or reality.get("short_ids") or ["0123456789abcdef"]
    sid = str(short_ids[0]) if short_ids else ""
    flow = str(first_client.get("flow") or "xtls-rprx-vision")

    params = {
        "encryption": "none",
        "flow": flow,
        "security": "reality",
        "sni": sni,
        "fp": fp,
        "pbk": pbk,
        "sid": sid,
        "type": "tcp",
        "headerType": "none",
    }
    q = urlencode(params, quote_via=quote)
    return f"vless://{uuid}@{host}:{port}?{q}"
