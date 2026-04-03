# xiaomi-be7000-setup

IaC-ориентированный конфигуратор для Xiaomi BE7000 (прошивка на базе OpenWrt): Docker Compose на USB, **Xray (VLESS+Reality)**, **mihomo**, **TorrServer**, автозапуск через UCI `firewall` include, бэкап/откат и smoke-проверки. Что умеет:
- устанавливает селективный Shadowsocks Proxy клиент [Mihomo](https://github.com/MetaCubeX/mihomo/tree/Alpha), с настройками маршрутизации на основе [re:filter](https://github.com/1andrevich/Re-filter-lists) и [Geosite](https://github.com/v2fly/domain-list-community/tree/master), до вашего Shadowsocks-сервера (для развертывания сервера можно использовать https://getoutline.org/ru/)
- [Mihomo Dashboard, The Official One, XD](https://github.com/MetaCubeX/metacubexd) — для мониторинга вашего Mihomo-клиента
- устанавливает Xray-сервер для того, чтобы вы могли подключаться к своему роутеру из внешней сети (например, со своего смартфона), используя роутер, как шлюз для Shadowsocks-прокси с маршрутизацией трафика
- вишенка: устанавливает [TorrServer](https://github.com/yourok/torrserver) для просмотра торрентов, например, со SmartTV

В итоге, ваш роутер сможет маршрутизировать трафик в домашней сети, обходя блокировки "с обоих сторон" (в том числе, сервисов, которые заблокировали доступ для российских пользователей) через Shadowsocks и проксируя напрямую весь отечественный трафик (банки, госсервесы). Помимо этого, если у вас есть белый IP, вы можете настроить роутер как Vless-сервер для подключения своих смартфонов, чтобы использовать настроенные правила маршрутизации без необходимости выключать proxy-клиент при использовании отечественных сервисов.

В качестве зависимости, конфигуратор использует [xmir-patcher](https://github.com/openwrt-xiaomi/xmir-patcher) (эксплойт/доступ к устройству и постоянный dropbear) для получения ssh-доступа к роутере.

## Требования

- **Python** 3.10+ и [Poetry](https://python-poetry.org/)
- [go-task](https://taskfile.dev/) (опционально, удобная обёртка над CLI)
- Роутер с уже включённым **Docker** через веб-интерфейс и привязанным **USB** (как хранилище mi_docker)
- Для первичного доступа без SSH — репозиторий [xmir-patcher](https://github.com/openwrt-xiaomi/xmir-patcher) (подключается как git submodule)

## Быстрый старт

```bash
git clone --recurse-submodules <url> xiaomi-be7000-setup
cd xiaomi-be7000-setup
poetry install
```

Скопируйте конфиги:

```bash
cp config/router.example.yaml config/router.yaml
cp config/router.secrets.example.yaml config/router.secrets.yaml
# Отредактируйте оба файла: host, пароль SSH, UUID, Reality-ключи, upstream для mihomo
```

Переменные окружения (перекрывают YAML): `ROUTER_HOST`, `ROUTER_SSH_PASSWORD`, `ROUTER_SSH_PORT`, `ROUTER_SSH_USER`, `ROUTER_PUBLIC_HOST`.

### SSH с нуля (xmir-patcher)

Если порт **22 закрыт**, после `git submodule update --init third_party/xmir-patcher`:

```bash
task bootstrap-ssh
# или
poetry run pip install -r third_party/xmir-patcher/requirements.txt
poetry run xiaomi-router bootstrap-ssh
```

Последовательно вызываются `connect.py <IP>` и `install_ssh.py` из [xmir-patcher](https://github.com/openwrt-xiaomi/xmir-patcher) (эксплойт/доступ к устройству и постоянный dropbear). Может понадобиться пароль веб-интерфейса Xiaomi — следуйте подсказкам скриптов upstream.

### Окружение на USB (однократно)

По желанию, с рабочего SSH:

```bash
task setup-opkg-usb
task setup-entware      # опционально, Entware в bind-mount /opt на USB
task setup-compose      # плагин docker compose на USB + опционально --write-profile
```

### Деплой стека

```bash
task deploy
# или
poetry run xiaomi-router deploy
```

Перед изменениями на USB создаётся архив в `$USB/backups/` и `uci export firewall`; при ошибке smoke выполняются `docker compose down`, затем откат файлов и импорт сохранённого `firewall`.

### Полезные команды

| Task / CLI | Назначение |
|------------|------------|
| `task render` | Локальный рендер в `build/rendered` (USB-заглушка) |
| `task render-live` | Рендер с определением USB по SSH |
| `task smoke` | Проверки портов и `docker compose` на роутере |
| `task sync-pull` | Скачать конфиги со стека в `build/synced-from-router` |
| `task sync-push` | Залить `build/rendered` и `docker compose up -d` |
| `task vless-link` | Напечатать VLESS (Reality) ссылку для клиента |
| `task rollback -- path/to/deploy-....json` | Откат по метаданным (JSON с роутера) |
| `task diagnose` | Сводка по mi_docker / USB |

Пример отката (JSON лежит на роутере в `$USB/backups/`):

```bash
scp root@192.168.31.1:/mnt/usb-*/backups/deploy-*.json ./
poetry run xiaomi-router rollback ./deploy-....json
```

## Структура репозитория

- `config/router.example.yaml` — основной пример (в git)
- `config/router.yaml` — ваш файл (в `.gitignore`)
- `config/router.secrets.yaml` — секреты (в `.gitignore`)
- `templates/` — Jinja2-шаблоны: xray, mihomo, compose, autorun-скрипты
- `src/xiaomi_router/` — Python CLI
- `third_party/xmir-patcher` — submodule

На USB создаётся каталог `stack/` (имя задаётся в `stack.relative_dir`): `docker-compose.yml`, `configs/xray`, `configs/mihomo`, `mihomo/mihomo-routing.sh`.

## VLESS и «белый» IP

Команда `vless-link` строит ссылку из `public_endpoint.host` (или `--host`) и секретов Reality. Для доступа **из интернета** нужны:

- проброс порта с WAN на хост роутера (порт inbound Xray, по умолчанию 443);
- у провайдера — публичный («белый») IP или статический адрес на вашем VPS, если вы выкладываете трафик иначе.

Роутер не обязан знать ваш внешний адрес — укажите его вручную в конфиге или в `--host`.

## Прозрачный прокси (iptables)

В `router.yaml` секция `routing.apply_iptables` по умолчанию `false`. Включайте только понимая последствия для вашей LAN (`routing.lan_cidr`).

## Ограничения Xiaomi Docker

Тома и проект Compose должны находиться под путями вида `/mnt/usb-*/…`.

## Лицензия

MIT
