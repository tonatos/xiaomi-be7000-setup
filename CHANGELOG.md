# Changelog

## [0.3.1] — 2026-04-21

- Для `v2rayA` в `transparent_mode=tun` добавлен `iptables FORWARD ACCEPT` из LAN в tun-интерфейс (`v2raya.tun_interface`, по умолчанию `tun+`)
- Добавлен `v2raya.tun_interface` в `router.base.yaml` и `router.example.yaml`
- В `smoke` добавлен WARN про включённый FakeDNS в `v2rayA` (`198.18.x.x` в `direct` при TUN)
- README дополнен рекомендациями для `v2rayA` TUN и параметром `v2raya.tun_interface`

## [0.3.0] — 2026-04-21

- Mihomo переведён на **TUN-режим** по умолчанию (`tun.enable: true`, `stack: mixed`, `auto-detect-interface: true`) — перехватывает TCP и UDP/QUIC без ручных iptables-правил
- Добавлены `cap_add: NET_ADMIN` и `devices: /dev/net/tun` в compose-шаблон для mihomo
- Добавлен `quic` в `mihomo.sniffer.sniffing` — sniffing QUIC Initial пакетов через TUN
- `block_quic` при TUN-режиме игнорируется (пишет warning в лог вместо DROP); FORWARD DROP применяется только без TUN
- v2rayA переведён на режим `transparent_mode: tun` по умолчанию (legacy `redirect` сохранён как опция)
- `lan-routing.sh` теперь учитывает `v2raya.transparent_mode`: в `tun` очищает старые REDIRECT-правила и не добавляет новые
- Создан `CHANGELOG.md`
