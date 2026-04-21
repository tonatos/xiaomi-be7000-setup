# Changelog

## [0.4.0] — 2026-04-21

- Добавлена команда `add-mihomo-proxy`: парсит ссылки `ss://`, `vless://`, `trojan://` и добавляет/обновляет прокси в `mihomo.proxies` файла `config/router.yaml` с сохранением комментариев (ruamel.yaml round-trip)
- Флаг `--print` выводит YAML-фрагмент для ручной вставки без изменения файла
- Флаг `--name` позволяет переопределить имя прокси из URL
- Добавлена зависимость `ruamel.yaml`

## [0.3.0] — 2026-04-21

- Mihomo переведён на **TUN-режим** по умолчанию (`tun.enable: true`, `stack: mixed`, `auto-detect-interface: true`) — перехватывает TCP и UDP/QUIC без ручных iptables-правил
- Добавлены `cap_add: NET_ADMIN` и `devices: /dev/net/tun` в compose-шаблон для mihomo
- Добавлен `quic` в `mihomo.sniffer.sniffing` — sniffing QUIC Initial пакетов через TUN
- `block_quic` при TUN-режиме игнорируется (пишет warning в лог вместо DROP); FORWARD DROP применяется только без TUN
- v2rayA переведён на режим `transparent_mode: tun` по умолчанию (legacy `redirect` сохранён как опция)
- `lan-routing.sh` теперь учитывает `v2raya.transparent_mode`: в `tun` очищает старые REDIRECT-правила и не добавляет новые
- Создан `CHANGELOG.md`
