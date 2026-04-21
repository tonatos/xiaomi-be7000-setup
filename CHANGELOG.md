# Changelog

## [0.5.0] — 2026-04-21

- Добавлен мастер конфигурации `xiaomi-router init` / `task init` на `Textual`: полноценный TUI-экран с секциями, формами и валидацией ввода
- Мастер сохраняет существующий `router.yaml` с round-trip YAML (без потери комментариев), обновляет только известные поля и не затирает пользовательские секции (`services.custom` и другие)
- Для сценария с `xray_server` мастер автоматически генерирует недостающие Reality-учётные данные и выводит готовую VLESS-ссылку для клиента

## [0.4.0] — 2026-04-21

- Добавлена команда `add-mihomo-proxy`: парсит ссылки `ss://`, `vless://`, `trojan://` и добавляет/обновляет прокси в `mihomo.proxies` файла `config/router.yaml` с сохранением комментариев (ruamel.yaml round-trip)
- Флаг `--print` выводит YAML-фрагмент для ручной вставки без изменения файла
- Флаг `--name` позволяет переопределить имя прокси из URL

## [0.3.0] — 2026-04-21

- Mihomo переведён на **TUN-режим** по умолчанию (`tun.enable: true`, `stack: mixed`, `auto-detect-interface: true`) — перехватывает TCP и UDP/QUIC без ручных iptables-правил
- Добавлены `cap_add: NET_ADMIN` и `devices: /dev/net/tun` в compose-шаблон для mihomo
- Добавлен `quic` в `mihomo.sniffer.sniffing` — sniffing QUIC Initial пакетов через TUN
- `block_quic` при TUN-режиме игнорируется (пишет warning в лог вместо DROP); FORWARD DROP применяется только без TUN
- v2rayA переведён на режим `transparent_mode: tun` по умолчанию (legacy `redirect` сохранён как опция)
- `lan-routing.sh` теперь учитывает `v2raya.transparent_mode`: в `tun` очищает старые REDIRECT-правила и не добавляет новые
- Создан `CHANGELOG.md`
