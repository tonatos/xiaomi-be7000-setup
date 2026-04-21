"""Парсинг proxy-ссылок (ss://, vless://, trojan://) в Mihomo proxy config dict.

Поддерживаемые форматы:
  ss://      — SIP002 (userinfo@host:port) и legacy (base64(method:password@host:port))
  vless://   — VLESS с параметрами transport/security/reality/ws/grpc/xhttp
  trojan://  — Trojan с параметрами transport/tls/ws/grpc
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b64decode_str(s: str) -> str:
    """Base64-decode (standard + urlsafe, ignores padding) → utf-8 str."""
    s = s.strip()
    # normalise: replace urlsafe chars back to standard, then add padding
    pad = s.replace("-", "+").replace("_", "/") + "=" * ((4 - len(s) % 4) % 4)
    return base64.b64decode(pad).decode("utf-8")


# ---------------------------------------------------------------------------
# Shadowsocks
# ---------------------------------------------------------------------------

def _parse_ss(url: str) -> dict[str, Any]:
    """Парсит Shadowsocks URL (SIP002 и legacy).

    SIP002: ss://userinfo@host:port/?plugin#tag
      где userinfo = base64(method:password) или method:password (percent-encoded)
    Legacy: ss://BASE64(method:password@host:port)#tag
      (entire netloc is base64; нет @ в netloc)
    """
    parsed = urlparse(url)
    name = unquote(parsed.fragment) if parsed.fragment else ""

    if "@" in parsed.netloc:
        # SIP002 — userinfo присутствует явно
        raw_userinfo = unquote(parsed.username or "")
        try:
            decoded = _b64decode_str(raw_userinfo)
            if ":" not in decoded:
                raise ValueError("no colon after decode")
            method, password = decoded.split(":", 1)
        except Exception:
            if ":" in raw_userinfo:
                method, password = raw_userinfo.split(":", 1)
            else:
                raise ValueError(
                    f"Не удалось разобрать userinfo SS URL: {raw_userinfo!r}"
                )

        host = parsed.hostname or ""
        port = parsed.port
        if not port:
            raise ValueError("SS URL: отсутствует порт")

        return {
            "name": name or f"ss-{host}",
            "type": "ss",
            "server": host,
            "port": port,
            "cipher": method,
            "password": password,
            "udp": True,
        }

    # Legacy: ss://BASE64(method:password@host:port)#tag
    body = url.split("//", 1)[1]
    fragment = ""
    if "#" in body:
        body, fragment = body.rsplit("#", 1)
    name = unquote(fragment) if fragment else name
    if "?" in body:
        body = body.split("?", 1)[0]
    body = body.rstrip("/")

    try:
        decoded = _b64decode_str(body)
    except Exception as exc:
        raise ValueError(f"SS URL: ошибка декодирования base64: {exc}") from exc

    if "@" not in decoded:
        raise ValueError(f"SS URL (legacy): ожидается @ в декодированной строке: {decoded!r}")

    userinfo, hostport = decoded.rsplit("@", 1)
    if ":" not in userinfo:
        raise ValueError(f"SS URL (legacy): userinfo без ':': {userinfo!r}")
    method, password = userinfo.split(":", 1)
    if ":" not in hostport:
        raise ValueError(f"SS URL (legacy): host:port без ':': {hostport!r}")
    host, port_s = hostport.rsplit(":", 1)

    return {
        "name": name or f"ss-{host}",
        "type": "ss",
        "server": host,
        "port": int(port_s),
        "cipher": method,
        "password": password,
        "udp": True,
    }


# ---------------------------------------------------------------------------
# VLESS
# ---------------------------------------------------------------------------

def _parse_vless(url: str) -> dict[str, Any]:
    """Парсит VLESS URL."""
    parsed = urlparse(url)
    name = unquote(parsed.fragment) if parsed.fragment else ""
    uuid = parsed.username or ""
    host = parsed.hostname or ""
    port = parsed.port
    if not port:
        raise ValueError("VLESS URL: отсутствует порт")

    params: dict[str, str] = {k: v[0] for k, v in parse_qs(parsed.query).items()}

    network = params.get("type", "tcp")
    security = params.get("security", "none")

    proxy: dict[str, Any] = {
        "name": name or f"vless-{host}",
        "type": "vless",
        "server": host,
        "port": port,
        "uuid": uuid,
        "network": network,
        "tls": security in ("tls", "reality"),
        "udp": True,
    }

    if flow := params.get("flow"):
        proxy["flow"] = flow

    sni = params.get("sni") or params.get("peer") or params.get("host")
    if sni and network not in ("ws", "xhttp", "h2"):
        # для ws/xhttp host — это заголовок, а не servername
        proxy["servername"] = sni
    elif sni and network not in ("ws", "xhttp", "h2"):
        proxy["servername"] = sni

    # SNI/servername для ws/xhttp/h2 берём только из sni/peer
    sni_only = params.get("sni") or params.get("peer")
    if sni_only and network in ("ws", "xhttp", "h2"):
        proxy["servername"] = sni_only

    if fp := params.get("fp"):
        proxy["client-fingerprint"] = fp

    if alpn_raw := params.get("alpn"):
        proxy["alpn"] = unquote(alpn_raw).split(",")

    if security == "reality":
        proxy["reality-opts"] = {
            "public-key": params.get("pbk", ""),
            "short-id": params.get("sid", ""),
        }

    if network == "ws":
        ws_opts: dict[str, Any] = {}
        if path := params.get("path"):
            ws_opts["path"] = unquote(path)
        if ws_host := params.get("host"):
            ws_opts["headers"] = {"Host": ws_host}
        if ws_opts:
            proxy["ws-opts"] = ws_opts

    elif network == "grpc":
        if svc := params.get("serviceName"):
            proxy["grpc-opts"] = {"grpc-service-name": unquote(svc)}

    elif network == "xhttp":
        xhttp_opts: dict[str, Any] = {}
        if path := params.get("path"):
            xhttp_opts["path"] = unquote(path)
        if xhost := params.get("host"):
            xhttp_opts["host"] = xhost
        if mode := params.get("mode"):
            xhttp_opts["mode"] = mode
        if xhttp_opts:
            proxy["xhttp-opts"] = xhttp_opts

    elif network == "h2":
        h2_opts: dict[str, Any] = {}
        if path := params.get("path"):
            h2_opts["path"] = unquote(path)
        if xhost := params.get("host"):
            h2_opts["host"] = [xhost]
        if h2_opts:
            proxy["h2-opts"] = h2_opts

    return proxy


# ---------------------------------------------------------------------------
# Trojan
# ---------------------------------------------------------------------------

def _parse_trojan(url: str) -> dict[str, Any]:
    """Парсит Trojan URL."""
    parsed = urlparse(url)
    name = unquote(parsed.fragment) if parsed.fragment else ""
    password = unquote(parsed.username or "")
    host = parsed.hostname or ""
    port = parsed.port
    if not port:
        raise ValueError("Trojan URL: отсутствует порт")

    params: dict[str, str] = {k: v[0] for k, v in parse_qs(parsed.query).items()}

    network = params.get("type", "tcp")

    proxy: dict[str, Any] = {
        "name": name or f"trojan-{host}",
        "type": "trojan",
        "server": host,
        "port": port,
        "password": password,
        "network": network,
        "tls": True,
        "udp": False,
    }

    if sni := params.get("sni") or params.get("peer"):
        proxy["sni"] = sni

    if fp := params.get("fp"):
        proxy["client-fingerprint"] = fp

    if alpn_raw := params.get("alpn"):
        proxy["alpn"] = unquote(alpn_raw).split(",")

    if params.get("allowInsecure") == "1":
        proxy["skip-cert-verify"] = True

    if network == "ws":
        ws_opts: dict[str, Any] = {}
        if path := params.get("path"):
            ws_opts["path"] = unquote(path)
        if ws_host := params.get("host"):
            ws_opts["headers"] = {"Host": ws_host}
        if ws_opts:
            proxy["ws-opts"] = ws_opts

    elif network == "grpc":
        if svc := params.get("serviceName"):
            proxy["grpc-opts"] = {"grpc-service-name": unquote(svc)}

    return proxy


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_proxy_url(url: str) -> dict[str, Any]:
    """Разобрать proxy-ссылку (ss://, vless://, trojan://) в Mihomo proxy dict."""
    url = url.strip()
    if url.startswith("ss://"):
        return _parse_ss(url)
    if url.startswith("vless://"):
        return _parse_vless(url)
    if url.startswith("trojan://"):
        return _parse_trojan(url)
    raise ValueError(
        f"Неподдерживаемый протокол: {url[:30]!r}. "
        "Ожидается ss://, vless:// или trojan://"
    )


def upsert_proxy_in_yaml(path: Path, proxy: dict[str, Any]) -> bool:
    """Добавить или обновить прокси в mihomo.proxies файла router.yaml.

    Использует ruamel.yaml для round-trip редактирования (сохраняет комментарии).

    Returns:
        True  — если прокси с таким именем уже существовал и был обновлён.
        False — если прокси был добавлен как новый.
    """
    from ruamel.yaml import YAML

    ryaml = YAML()
    ryaml.preserve_quotes = True
    ryaml.width = 4096

    with path.open(encoding="utf-8") as f:
        data = ryaml.load(f)

    if data is None:
        data = {}

    mihomo = data.setdefault("mihomo", {})
    if not isinstance(mihomo, dict):
        raise ValueError("mihomo в router.yaml — не объект")

    proxies = mihomo.get("proxies")
    if proxies is None or not isinstance(proxies, list):
        mihomo["proxies"] = []
        proxies = mihomo["proxies"]

    proxy_name = proxy["name"]
    updated = False
    for i, existing in enumerate(proxies):
        if isinstance(existing, dict) and existing.get("name") == proxy_name:
            proxies[i] = proxy
            updated = True
            break

    if not updated:
        proxies.append(proxy)

    with path.open("w", encoding="utf-8") as f:
        ryaml.dump(data, f)

    return updated
