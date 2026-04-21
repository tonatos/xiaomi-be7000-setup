from __future__ import annotations

import base64
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from rich.console import Console
from rich.panel import Panel

from xiaomi_router.config_loader import load_merged_config, validate_merged_config_for_deploy
from xiaomi_router.paths import default_config_path, default_secrets_path, repo_root
from xiaomi_router.pipeline import deploy
from xiaomi_router.proxy_url_parser import parse_proxy_url
from xiaomi_router.vless_link import build_vless_reality_link
from xiaomi_router.xmir_bootstrap import run_bootstrap_if_needed

KNOWN_SERVICES: dict[str, tuple[str, str]] = {
    "mihomo": (
        "Mihomo client",
        "Proxy-клиент с transparent proxy (TUN), правилами и DNS.",
    ),
    "v2raya": (
        "v2rayA client",
        "Proxy-клиент с web UI и управлением outbound через GUI.",
    ),
    "metacubexd": (
        "Mihomo dashboard",
        "Web-панель мониторинга и управления Mihomo.",
    ),
    "xray_server": (
        "VLESS server (Xray Reality)",
        "Внешний вход для клиентов смартфона/ноутбука через роутер.",
    ),
    "adguardhome": (
        "AdGuard Home",
        "Локальный DNS-фильтр рекламы/трекеров с интеграцией в dnsmasq.",
    ),
    "torrserver": (
        "TorrServer",
        "Стриминг торрент-контента в домашней сети.",
    ),
}

DEFAULT_FLOWS: dict[str, str] = {
    "xray_client_flow": "xtls-rprx-vision",
}


@dataclass
class InitWizardAnswers:
    host: str
    ssh_password: str
    proxy_client: str
    selected_services: list[str]
    proxy_url: str
    public_host: str
    should_root: bool
    should_deploy: bool


def _ensure_tui_packages() -> None:
    try:
        import textual  # type: ignore # noqa: F401
    except ModuleNotFoundError:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "textual", "rich"],
            check=False,
        )
        try:
            import textual  # type: ignore # noqa: F401
        except ModuleNotFoundError as exc:
            raise SystemExit("Не удалось установить textual для запуска TUI-мастера.") from exc


def _run_local_command(args: list[str], *, cwd: Path, step: str) -> None:
    result = subprocess.run(args, cwd=cwd, check=False)
    if result.returncode != 0:
        cmd = " ".join(args)
        raise SystemExit(f"{step} завершился с ошибкой ({result.returncode}): {cmd}")


def _ensure_xmir_dependencies(console: Any) -> None:
    root = repo_root()
    xmir = root / "third_party" / "xmir-patcher"
    req = xmir / "requirements.txt"

    if not req.exists():
        console.print("[yellow]Инициализирую submodule xmir-patcher...[/yellow]")
        _run_local_command(
            ["git", "submodule", "update", "--init", "third_party/xmir-patcher"],
            cwd=root,
            step="Инициализация xmir-patcher",
        )

    if req.exists():
        console.print("[yellow]Проверяю Python-зависимости xmir-patcher...[/yellow]")
        _run_local_command(
            [sys.executable, "-m", "pip", "install", "-q", "-r", str(req)],
            cwd=root,
            step="Установка зависимостей xmir-patcher",
        )


def _ensure_router_yaml_exists(config_path: Path, console: Any) -> None:
    if config_path.exists():
        return
    example = config_path.parent / "router.example.yaml"
    if not example.exists():
        raise SystemExit(
            f"Не найден ни {config_path}, ни шаблон {example} для инициализации."
        )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    console.print(f"[green]Создан {config_path} из шаблона {example}.[/green]")


def _yaml_rt() -> Any:
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    return yaml


def _load_roundtrip_yaml(path: Path) -> dict[str, Any]:
    from ruamel.yaml.comments import CommentedMap

    yaml = _yaml_rt()
    with path.open(encoding="utf-8") as f:
        data = yaml.load(f)
    if data is None:
        return CommentedMap()
    if not isinstance(data, CommentedMap):
        raise SystemExit(
            f"{path}: в корне YAML ожидается объект (mapping), а не {type(data).__name__}."
        )
    return data


def _dump_roundtrip_yaml(path: Path, data: dict[str, Any]) -> None:
    yaml = _yaml_rt()
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f)


def _services_defaults(merged_cfg: dict[str, Any]) -> list[str]:
    services = merged_cfg.get("services")
    if not isinstance(services, dict):
        return []
    selected: list[str] = []
    for key in KNOWN_SERVICES:
        svc = services.get(key)
        if isinstance(svc, dict) and bool(svc.get("enabled", False)):
            selected.append(key)
    return selected


def _normalize_services(proxy_client: str, selected: list[str]) -> list[str]:
    picked = set(selected)
    if proxy_client == "mihomo":
        picked.add("mihomo")
        picked.discard("v2raya")
    else:
        picked.add("v2raya")
        picked.discard("mihomo")
    return list(picked)


def _ensure_mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
    from ruamel.yaml.comments import CommentedMap

    current = parent.get(key)
    if isinstance(current, CommentedMap):
        return current
    if isinstance(current, dict):
        mapped = CommentedMap(current)
        parent[key] = mapped
        return mapped
    mapped = CommentedMap()
    parent[key] = mapped
    return mapped


def _ensure_sequence(parent: dict[str, Any], key: str) -> list[Any]:
    from ruamel.yaml.comments import CommentedSeq

    current = parent.get(key)
    if isinstance(current, CommentedSeq):
        return current
    if isinstance(current, list):
        seq = CommentedSeq(current)
        parent[key] = seq
        return seq
    seq = CommentedSeq()
    parent[key] = seq
    return seq


def _is_placeholder_uuid(value: str) -> bool:
    return value.strip() in {"", "00000000-0000-0000-0000-000000000000"}


def _is_placeholder_private_key(value: str) -> bool:
    v = value.strip()
    return not v or "REPLACE_WITH" in v or "YOUR_" in v


def _gen_reality_private_key_b64url() -> str:
    priv = x25519.X25519PrivateKey.generate()
    raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _ensure_xray_credentials(data: dict[str, Any]) -> None:
    from ruamel.yaml.comments import CommentedMap

    xray = _ensure_mapping(data, "xray")
    clients = _ensure_sequence(xray, "clients")

    first_client: CommentedMap
    if clients and isinstance(clients[0], dict):
        if isinstance(clients[0], CommentedMap):
            first_client = clients[0]
        else:
            first_client = CommentedMap(clients[0])
            clients[0] = first_client
    else:
        first_client = CommentedMap()
        clients.insert(0, first_client)

    client_id = str(first_client.get("id", "")).strip()
    if _is_placeholder_uuid(client_id):
        first_client["id"] = str(uuid.uuid4())
    first_client.setdefault("flow", DEFAULT_FLOWS["xray_client_flow"])

    reality = _ensure_mapping(xray, "reality")
    private_key = str(reality.get("private_key", "")).strip()
    if _is_placeholder_private_key(private_key):
        reality["private_key"] = _gen_reality_private_key_b64url()

    short_ids = _ensure_sequence(reality, "short_ids")
    if not short_ids:
        short_ids.append(os.urandom(8).hex())


def _upsert_mihomo_proxy(data: dict[str, Any], proxy: dict[str, Any]) -> None:
    mihomo = _ensure_mapping(data, "mihomo")
    proxies = _ensure_sequence(mihomo, "proxies")
    name = proxy.get("name")
    for idx, existing in enumerate(proxies):
        if isinstance(existing, dict) and existing.get("name") == name:
            proxies[idx] = proxy
            return
    proxies.append(proxy)


def _run_textual_wizard(defaults: dict[str, Any]) -> InitWizardAnswers | None:
    _ensure_tui_packages()
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical
    from textual.widgets import Button, Checkbox, Footer, Header, Input, Select, Static

    class InitWizardApp(App[InitWizardAnswers | None]):
        CSS = """

        #wizard {
            width: 100%;
            height: 1fr;
            padding: 1 3;
        }

        #title {
            padding-bottom: 1;
            text-style: bold;
        }

        #progress {
            margin-bottom: 1;
        }

        .wizard-step {
            padding: 1;
            height: 1fr;
            display: none;
        }

        .section-title {
            text-style: bold;
            margin-bottom: 1;
        }

        .helptext {
            margin: 0 0 1 0;
        }

        Input, Select {
            margin: 0 0 1 0;
        }

        #actions {
            height: auto;
            margin-top: 1;
        }
        """

        BINDINGS = [("ctrl+c", "quit", "Выход")]
        STEP_IDS = [
            "step_router",
            "step_proxy",
            "step_services",
            "step_xray",
            "step_actions",
        ]
        STEP_TITLES = [
            "Доступ к роутеру",
            "Клиент прокси",
            "Сервисы",
            "Xray / VLESS",
            "Шаги выполнения",
        ]
        _step_index = 0

        def compose(self) -> ComposeResult:
            service_defaults = set(defaults["selected_services"])
            yield Header(show_clock=True)
            with Vertical(id="wizard"):
                yield Static(
                    "Xiaomi BE7000 Init Wizard\n"
                    "Пошаговая инициализация: конфиг, root/bootstrap и deploy.",
                    id="title",
                )
                yield Static("", id="progress")

                with Vertical(classes="wizard-step", id="step_router"):
                    yield Static("Доступ к роутеру", classes="section-title")
                    yield Static(
                        "IP или hostname роутера в вашей LAN. Обычно это 192.168.31.1; "
                        "значение используется для SSH, bootstrap и deploy.",
                        classes="helptext",
                    )
                    yield Input(defaults["host"], placeholder="router host", id="host")
                    yield Static(
                        "Пароль root по SSH. Нужен для проверки состояния роутера, настройки "
                        "окружения и выкладки стека. Если не знаете пароль — сначала выполните bootstrap.",
                        classes="helptext",
                    )
                    yield Input(
                        defaults["ssh_password"],
                        placeholder="SSH password",
                        password=True,
                        id="ssh_password",
                    )

                with Vertical(classes="wizard-step", id="step_proxy"):
                    yield Static("Клиент прокси", classes="section-title")
                    yield Static(
                        "Выберите движок transparent proxy. От выбора зависит, какие сервисы будут "
                        "включены и какие настройки попадут в итоговый конфиг.",
                        classes="helptext",
                    )
                    yield Select(
                        options=[
                            ("mihomo - transparent proxy TUN + rules", "mihomo"),
                            ("v2raya - web GUI клиент", "v2raya"),
                        ],
                        value=defaults["proxy_client"],
                        id="proxy_client",
                    )
                    yield Static(
                        "Ссылка вашего upstream-прокси для Mihomo (форматы: ss://, vless://, trojan://). "
                        "Её выдает ваш VPS/панель провайдера. Если заполнить поле, ссылка будет добавлена "
                        "в секцию mihomo.proxies в router.yaml.",
                        classes="helptext",
                    )
                    yield Input(
                        "",
                        placeholder="Upstream URL для Mihomo (ss://, vless://, trojan://). Пусто = пропустить",
                        id="proxy_url",
                    )

                with Vertical(classes="wizard-step", id="step_services"):
                    yield Static("Сервисы", classes="section-title")
                    yield Static(
                        "Выберите, какие встроенные контейнеры развернуть. Это влияет на docker-compose, "
                        "autorun-скрипты и smoke-проверки. Можно оставить только нужный минимум.",
                        classes="helptext",
                    )
                    for service_name, (title, description) in KNOWN_SERVICES.items():
                        yield Checkbox(
                            f"{title}: {description}",
                            value=service_name in service_defaults,
                            id=f"svc_{service_name}",
                        )

                with Vertical(classes="wizard-step", id="step_xray"):
                    yield Static("Xray/VLESS (если включён xray_server)", classes="section-title")
                    yield Static(
                        "Публичный IP или домен, по которому ваши внешние клиенты будут подключаться к "
                        "VLESS на роутере. Обычно это белый IP провайдера или домен с DNS-записью на него.",
                        classes="helptext",
                    )
                    yield Input(
                        defaults["public_host"],
                        placeholder="Публичный IP/домен для public_endpoint.host",
                        id="public_host",
                    )

                with Vertical(classes="wizard-step", id="step_actions"):
                    yield Static("Шаги выполнения", classes="section-title")
                    yield Static(
                        "Bootstrap поднимает/проверяет SSH через xmir-patcher. Deploy применяет конфиг: "
                        "рендерит шаблоны, загружает файлы на роутер и запускает контейнеры.",
                        classes="helptext",
                    )
                    yield Checkbox(
                        "Запустить root/bootstrap SSH (xmir-patcher)",
                        value=True,
                        id="should_root",
                    )
                    yield Checkbox(
                        "Сразу выполнить deploy после сохранения",
                        value=True,
                        id="should_deploy",
                    )

                with Horizontal(id="actions"):
                    yield Button("Назад", id="back")
                    yield Button("Далее", id="next")
                    yield Button("Сохранить и продолжить", id="submit")
                    yield Button("Отмена", id="cancel")
            yield Footer()

        def on_mount(self) -> None:
            self.title = "Xiaomi BE7000 Setup"
            self.sub_title = " Мастер конфигурации Xiaomi BE7000"
            self.theme = "gruvbox"
            self._enforce_proxy_client_services()
            self._show_step(0)

        def on_select_changed(self, event: Select.Changed) -> None:
            if event.select.id == "proxy_client":
                self._enforce_proxy_client_services()

        def _service_checkbox(self, key: str) -> Checkbox:
            return self.query_one(f"#svc_{key}", Checkbox)

        def _enforce_proxy_client_services(self) -> None:
            proxy_client = str(self.query_one("#proxy_client", Select).value)
            mihomo_cb = self._service_checkbox("mihomo")
            v2raya_cb = self._service_checkbox("v2raya")
            if proxy_client == "mihomo":
                mihomo_cb.value = True
                mihomo_cb.disabled = True
                v2raya_cb.value = False
                v2raya_cb.disabled = True
            else:
                v2raya_cb.value = True
                v2raya_cb.disabled = True
                mihomo_cb.value = False
                mihomo_cb.disabled = True

        def _update_progress(self) -> None:
            progress = self.query_one("#progress", Static)
            progress.update(
                f"Шаг {self._step_index + 1}/{len(self.STEP_IDS)}: "
                f"{self.STEP_TITLES[self._step_index]}"
            )

        def _show_step(self, index: int) -> None:
            self._step_index = max(0, min(index, len(self.STEP_IDS) - 1))
            for idx, step_id in enumerate(self.STEP_IDS):
                step = self.query_one(f"#{step_id}", Vertical)
                step.display = idx == self._step_index

            back = self.query_one("#back", Button)
            next_btn = self.query_one("#next", Button)
            submit = self.query_one("#submit", Button)
            back.disabled = self._step_index == 0
            next_btn.display = self._step_index < len(self.STEP_IDS) - 1
            submit.display = self._step_index == len(self.STEP_IDS) - 1
            self._update_progress()

        def _validate_current_step(self) -> bool:
            if self._step_index == 0:
                host = self.query_one("#host", Input).value.strip()
                if not host:
                    self.notify("router.host не может быть пустым", severity="error")
                    return False
            elif self._step_index == 1:
                proxy_client = str(self.query_one("#proxy_client", Select).value).strip()
                proxy_url = self.query_one("#proxy_url", Input).value.strip()
                if proxy_client == "mihomo" and proxy_url:
                    try:
                        parse_proxy_url(proxy_url)
                    except ValueError as exc:
                        self.notify(f"Некорректная proxy URL: {exc}", severity="error")
                        return False
            elif self._step_index == 2:
                proxy_client = str(self.query_one("#proxy_client", Select).value).strip()
                selected_services: list[str] = []
                for service_name in KNOWN_SERVICES:
                    if self._service_checkbox(service_name).value:
                        selected_services.append(service_name)
                if not _normalize_services(proxy_client, selected_services):
                    self.notify("Выберите хотя бы один сервис.", severity="error")
                    return False
            elif self._step_index == 3:
                proxy_client = str(self.query_one("#proxy_client", Select).value).strip()
                public_host = self.query_one("#public_host", Input).value.strip()
                selected_services: list[str] = []
                for service_name in KNOWN_SERVICES:
                    if self._service_checkbox(service_name).value:
                        selected_services.append(service_name)
                selected_services = _normalize_services(proxy_client, selected_services)
                if "xray_server" in selected_services and not public_host:
                    self.notify(
                        "Для xray_server заполните публичный IP/домен.",
                        severity="error",
                    )
                    return False
            return True

        def _collect_answers(self) -> InitWizardAnswers:
            host = self.query_one("#host", Input).value.strip()
            proxy_client = str(self.query_one("#proxy_client", Select).value).strip()
            proxy_url = self.query_one("#proxy_url", Input).value.strip()
            public_host = self.query_one("#public_host", Input).value.strip()
            selected_services: list[str] = []
            for service_name in KNOWN_SERVICES:
                if self._service_checkbox(service_name).value:
                    selected_services.append(service_name)
            selected_services = _normalize_services(proxy_client, selected_services)
            ssh_password = self.query_one("#ssh_password", Input).value.strip()
            should_root = self.query_one("#should_root", Checkbox).value
            should_deploy = self.query_one("#should_deploy", Checkbox).value
            return InitWizardAnswers(
                host=host,
                ssh_password=ssh_password,
                proxy_client=proxy_client,
                selected_services=selected_services,
                proxy_url=proxy_url,
                public_host=public_host,
                should_root=should_root,
                should_deploy=should_deploy,
            )

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "cancel":
                self.exit(None)
                return
            if event.button.id == "back":
                self._show_step(self._step_index - 1)
                return
            if event.button.id == "next":
                if self._validate_current_step():
                    self._show_step(self._step_index + 1)
                return
            if event.button.id != "submit":
                return

            if not self._validate_current_step():
                return

            self.exit(self._collect_answers())

    app = InitWizardApp()
    return app.run()


def run_init_wizard(config: Path | None = None, secrets: Path | None = None) -> None:
    console = Console()
    config_path = config or default_config_path()
    _ensure_router_yaml_exists(config_path, console)

    sec_path = secrets
    if sec_path is None and default_secrets_path().exists():
        sec_path = default_secrets_path()

    source = _load_roundtrip_yaml(config_path)
    merged = load_merged_config(config_path, sec_path)

    console.print(Panel.fit("Запускаю Textual-мастер...", border_style="bright_blue"))

    router_cfg = merged.get("router", {}) if isinstance(merged.get("router"), dict) else {}
    user_default = str(router_cfg.get("ssh_user", "root"))
    port_default = int(router_cfg.get("ssh_port", 22))
    public_endpoint = merged.get("public_endpoint")
    answers = _run_textual_wizard(
        {
            "host": str(router_cfg.get("host", "192.168.31.1")),
            "ssh_password": str(router_cfg.get("ssh_password", "")).strip(),
            "proxy_client": (
                str(merged.get("proxy_client", "mihomo")).strip()
                if str(merged.get("proxy_client", "mihomo")).strip() in {"mihomo", "v2raya"}
                else "mihomo"
            ),
            "selected_services": _services_defaults(merged),
            "public_host": (
                str(public_endpoint.get("host", "")).strip()
                if isinstance(public_endpoint, dict)
                else ""
            ),
        }
    )
    if answers is None:
        raise SystemExit("Мастер прерван пользователем.")

    proxy_payload: dict[str, Any] | None = None
    if answers.proxy_client == "mihomo" and answers.proxy_url:
        try:
            proxy_payload = parse_proxy_url(answers.proxy_url)
        except ValueError as exc:
            raise SystemExit(f"Некорректная ссылка upstream-прокси: {exc}") from exc

    router = _ensure_mapping(source, "router")
    router["host"] = answers.host
    router["ssh_user"] = user_default
    router["ssh_port"] = port_default
    if answers.ssh_password:
        router["ssh_password"] = answers.ssh_password

    source["proxy_client"] = answers.proxy_client

    services = _ensure_mapping(source, "services")
    for service_name in KNOWN_SERVICES:
        svc = _ensure_mapping(services, service_name)
        svc["enabled"] = service_name in answers.selected_services

    if answers.public_host:
        public_endpoint_map = _ensure_mapping(source, "public_endpoint")
        public_endpoint_map["host"] = answers.public_host

    if "xray_server" in answers.selected_services:
        _ensure_xray_credentials(source)

    if proxy_payload is not None:
        _upsert_mihomo_proxy(source, proxy_payload)
        console.print("[green]Upstream-прокси добавлен/обновлён в mihomo.proxies.[/green]")

    _dump_roundtrip_yaml(config_path, source)
    console.print(f"[green]Конфиг обновлён:[/green] {config_path}")

    merged_after = load_merged_config(config_path, sec_path)
    try:
        validate_merged_config_for_deploy(merged_after)
    except ValueError as exc:
        console.print(
            "[yellow]Конфиг сохранён, но есть предупреждение перед deploy:[/yellow]\n"
            f"{exc}"
        )
        if answers.should_deploy:
            raise SystemExit("Исправьте конфиг и повторите deploy.")

    if answers.should_root:
        if not answers.ssh_password:
            raise SystemExit(
                "Для bootstrap/deploy нужен пароль SSH. Укажите router.ssh_password."
            )
        _ensure_xmir_dependencies(console)
        run_bootstrap_if_needed(
            answers.host,
            ssh_port=port_default,
            ssh_password=answers.ssh_password,
            ssh_user=user_default,
            force=False,
        )

    if answers.should_deploy:
        console.print("[cyan]Запускаю deploy...[/cyan]")
        deploy(merged_after, log=lambda line: console.print(line, markup=False))
        console.print("[green]Deploy завершён успешно.[/green]")

    if "xray_server" in answers.selected_services:
        try:
            link = build_vless_reality_link(
                merged_after, override_host=answers.public_host or None
            )
            console.print("\n[bold green]Параметры подключения VLESS (Reality):[/bold green]")
            console.print(link)
        except SystemExit as exc:
            console.print(
                "[yellow]Xray включён, но ссылку VLESS пока не удалось собрать:[/yellow]\n"
                f"{exc}"
            )
