# Changelog

## [0.6.2] — 2026-04-24

- В `routing/lan-routing.sh` добавлен опциональный runtime-тюнинг `routing.performance_tuning` (RPS/XPS/RFS): настройка `rps_cpus`, `xps_cpus`, `rps_sock_flow_entries` и `rps_flow_cnt` для выбранных интерфейсов, с автоматическим применением на `start` и откатом на `stop`
- Для `performance_tuning` добавлено неблокирующее ожидание появления `Meta`: если интерфейс ещё не поднят на `start`, запускается фоновый watcher (`meta_wait_seconds`), который применяет tuning после появления интерфейса
- В `config/router.base.yaml` и `config/router.example.yaml` добавлена секция `routing.performance_tuning` с дефолтами для Xiaomi BE7000 (маска CPU, список интерфейсов, RFS-параметры)
- В `README.md` добавлена инструкция по включению `performance_tuning` для случаев, когда TUN/REDIRECT упирается в `softirq`
- В `lan-routing.sh` добавлен управляемый DNS REDIRECT для `mihomo`: LAN DNS (TCP/UDP 53) принудительно перенаправляется на `mihomo` (`routing.mihomo_dns_port`, по умолчанию `1053`), чтобы fake-ip и доменные правила применялись ко всем клиентам LAN независимо от апстримов `dnsmasq`
- В `router.base.yaml` добавлены параметры `routing.mihomo_dns_redirect` (по умолчанию `true`) и `routing.mihomo_dns_port` для явного контроля DNS-пайплайна
- В README добавлено описание DNS REDIRECT-поведения для TUN-режима `mihomo`

## [0.6.1] — 2026-04-22

- В `autorun/010-start-docker.sh` добавлен auto-swap bootstrap: перед запуском Docker проверяется `SwapTotal`, при необходимости выполняется `swapon` существующего `/mnt/usb*/.swapfile`, а если файла нет — создаётся и инициализируется swapfile 256MB на USB (на случай, если этого не сделали через интерфейс, иначе память не хватает на запуск стека)
- В `deploy` добавлено авто-правило UCI `firewall.xray_vless_wan_allow` (WAN ACCEPT для tcp-порта `xray.inbound.port`, по умолчанию 443/8443), чтобы VLESS inbound был доступен извне при `services.xray_server.enabled: true` (после сброса прошивки и накатывания конфигурации с этим возникли проблемы)
- В `smoke` проверка WAN-правила для Xray переведена на общий `iptables -S` (а не только `zone_wan_input`), чтобы избежать ложных WARN, когда ACCEPT-правило находится в `input_wan_rule`

## [0.6] — 2026-04-22

- Для TorrServer дефолтный образ переключён на `ghcr.io/yourok/torrserver:latest` и обновлены env-переменные в compose (`TS_CONF_PATH`/`TS_LOG_PATH`/`TS_TORR_DIR`), чтобы бинарь использовался из образа и не скачивался на каждый старт
- Autorun маршрутизации: один файл `020-proxy-routing.sh` под выбранный `proxy_client` (mihomo или v2raya); отдельные `020-mihomo-routing.sh` / `021-v2raya-routing.sh` не генерируются; при `deploy` старые имена удаляются с роутера
- TorrServer runtime вынесен из `stack/configs`: по умолчанию в compose используются `runtime_volume` (`../torrserver-runtime:/opt/torrserver`) и отдельный read-only mount для `settings.json`; это убирает ядро/логи/кэш/torrents из stack-бэкапа
- AdGuard Home runtime вынесен из `stack/configs`: `services.adguardhome.work_dir` по умолчанию теперь `../adguardhome-runtime/work`, чтобы `work/data` не раздувал stack и бэкапы
- Runtime `mihomo` и `v2raya` вынесены из `stack/configs` (`../mihomo-runtime`, `../v2raya-runtime`); для `mihomo` конфиг монтируется отдельным read-only файлом `./configs/mihomo-config/config.yaml`
- На boot добавлен `005-ensure-shell-env.sh`: восстанавливает hook `usb-env` в `/etc/profile`, если прошивка перезаписала файл после ребута
- В backup stack-архива исключены runtime-хвосты в `configs/torrserver` (`TorrServer-*`, `torrents`, `cache`, `log`, `*.log`), legacy `configs/adguardhome/work`, а также legacy `configs/mihomo` и `configs/v2raya`, чтобы не раздувать backup и не ловить таймауты deploy

## [0.5.2] — 2026-04-21
- В compose-шаблоне для TorrServer добавлены поддержка лимитов контейнера (`mem_limit`, `pids_limit`, `ulimits`) и параметров путей/логов
- Добавлен рендер `configs/torrserver/config/settings.json` из YAML-конфига (`torrserver.settings`)


## [0.5.1] — 2026-04-21
- Поддерживается ветвление в случае, если пароль от SSH не задан
- для V2raya не требует ссылку для прокси-сервера
- пофикшен DNS на роутере

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
