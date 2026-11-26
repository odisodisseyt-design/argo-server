Ты — старший DevOps/инженер инструментария. Ниже — факты, собранные read-only с сервера разработки. Задача: проанализировать текущую среду и предложить, как модернизировать OpenCode и сделать его максимально эффективным инструментом разработки. Отвечай по-русски, структурированно, с приоритетами. Не предлагай платные сервисы и ничего, требующего GPU. Бюджет — 0 долларов, предпочтение — свободное ПО. Не выдумывай факты: если данных нет, скажи об этом.

## Ограничения сбора
Ничего не устанавливалось, не обновлялось, не удалялось; конфигурации, службы и файлы проекта не менялись. Секреты заменены на `***`.

## 1. Сервер и ОС
- Ubuntu 26.04.1 LTS (Resolute Raccoon), ядро Linux 7.0.0-43-generic, x86-64, KVM (Red Hat, i440FX).
- Hostname: `v3310666.hosted-by-vdsina.ru`. Uptime: 1 день 23 ч 29 мин.
- CPU: 4 ядра, `Common KVM processor`, 1 поток/ядро, 1 сокет. GPU нет (по заданным характеристикам).
- RAM: 7,2 GiB всего; 1,6 GiB-used; 3,1 GiB free; 3,5 GiB buff/cache; 5,7 GiB доступно. Swap = 0.
- Диск `/`: 148 G, занято 28 G, свободно 115 G (20 %). `/tmp` tmpfs 3,7 G (664 M занято).
- Load average: 0,12 / 0,16 / 0,17. Снимок top: 14,3 % us, 8,2 % sy, 73,5 % id, 2,0 % wa, 2,0 % st; 134 задачи (1 running).
- Топ по CPU/памяти: `containerd` 8,3 %, `opencode` PID 52973 8,3 % CPU и ~378 MB RSS (~5 % RAM), `kworker` 8,3 %.
- Активные службы (25): caddy, chrony, containerd, cron, dbus, docker, fwupd, getty@tty1, ModemManager, multipathd, networkd-dispatcher, polkit, rsyslog, ssh, systemd-hostnamed, systemd-journald, systemd-logind, systemd-networkd, systemd-resolved, systemd-udevd, text-world-ollama, text-world, udisks2, upower, user@0.
- 14 systemd-таймеров: sysstat-collect, fwupd-refresh, update-notifier-download, systemd-tmpfiles-clean, motd-news, dpkg-db-backup, sysstat-rotate, sysstat-summary, logrotate, fstrim, man-db, update-notifier-motd, e2scrub_all, xfs_scrub_all.

## 2. OpenCode
- Версия `v2.3.2`. Бинарник `/root/.opencode/bin/opencode` (252 848 304 байт), обёртка `/root/.opencode/bin/opencode2` (47 байт).
- Запущен как фоновая служба: `/root/.opencode/bin/opencode serve --service`, PID 52973. Адрес `http://127.0.0.1:49374`; публичный туннель `https://2a36e3fe2c92a48f.mtytxjbokf4t.opentunnel.xyz`. `/api/info`: `persistentPty: true`, tmp = `/tmp/opencode`.
- Глобальный конфиг: `/root/.config/opencode/opencode.json` (1428 байт), `$schema: https://opencode.ai/config.json`.
  - providers: `cheapllm` (openai-compatible, env `CHEAPLLM_API_KEY`, baseURL `https://cheapllm.shop/v1`, модель `deepseek-v4.1-flash-1017`); `gemini-relay` (провайдер google, baseURL Cloudflare Playground, модель `gemini-3.5-flash-lite`, лимит 1 048 576 input / 65 536 output, apiKey и кастомный заголовок — значения заредктированы).
  - agents: `nvidia-coder` (mode `subagent`, `opencode/nemotron-3.5-super-vl-free#high`), `general` (та же модель).
  - Полей `model`, `permissions`, `mcp`, `skills`, `plugins`, `instructions`, `formatter`, `lsp` — нет.
  - Факт: учётные данные `gemini-relay` хранятся в открытом виде в конфиге.
- `/root/.config/opencode/service.json`: remote route, `remoteAccepted`, env с ключами и password (значения заредктированы).
- Проектный конфиг ARGO: `/root/text-world/opencode.json`, `/root/text-world/.opencode/opencode.json`, `/root/text-world/android/opencode.json` — отсутствуют. `/root/.config/opencode/cli.json` — отсутствует.
- `AGENTS.md`: `/root/opencode/AGENTS.md` (2933 байт) и `/root/game-studio/AGENTS.md` — оба вне каталога ARGO.
- Плагины (`opencode plugin list`): `hide-paid-models` (local, `/root/.config/opencode/plugins/hide-paid-models.js`) — удаляет из списка модели с ненулевой ценой, исключения `gemini-relay` и `freellmapi`; `model-availability` (local, `/root/.config/opencode/plugins/model-availability/{index.ts,tui.tsx,package.json}` v1.0.0) — ставит `✓`/`✗` и cooldown при 429/rate limit. Рядом лежит `/root/.config/opencode/plugins/redeploy-gemini-relay.sh` (не плагин).
- MCP: `opencode mcp list` → `No MCP servers configured`; `list_mcp_resources` → resources [], templates [].
- Skills runtime: `opencode`, `report` (встроенные, файлов `SKILL.md` на диске не найдено). 27 файлов `SKILL.md` в `/root/game-studio/.claude/skills/` относятся к Claude Code другого проекта.
- Агенты: `nvidia-coder`, `general` из конфига; `explore` объявляется runtime без модели в конфиге. `opencode api get /api/agent` → `{}`. Команды `opencode agent list` не существует.
- Разрешения: поле `permissions` не задано; конкретный allow/deny список получить нельзя. Есть флаг CLI `--auto`.
- Инструменты сессии (13): edit, experimental_image_generate, glob, grep, question, read, shell, skill, subagent, webfetch, websearch, write, execute.
- Code Mode (`tools.opencode.*`, внутри `execute`, 8): session_move, worktree_create, worktree_list, worktree_remove, list_mcp_resources, models, read_mcp_resource, session_rename.

## 3. Рабочая среда
- Git 2.53.0; GitHub CLI 2.46.0; вход в github.com как `odisodisseyt-design`, scopes `gist`, `read:org`, `repo`; credential helper `!/usr/bin/gh auth git-credential`; `gh api user` подтверждён.
- Shell: `/bin/bash`; доступны sh, bash, rbash, dash, screen, tmux. tmux 3.6, Screen 4.09.01, ssh.service активен.
- Языки/инструменты: Python 3.14.4 + pip 26.3; Rust/Cargo 1.93.1; GCC 15.2.0; Java 17.0.21.1+2 (Temurin) в `/opt/jdk17/jdk-17.0.21.1+2` (в PATH отсутствует); Gradle 8.9 в `/opt/gradle-8.9` (Kotlin 1.9.23, требует JAVA_HOME); Android SDK `/opt/android-sdk` (platform android-35, build-tools 34.0.0/35.0.0, cmdline-tools latest).
- Отсутствуют: Node.js/npm/npx, Go, G++, CMake, make, бинарник bun (`/root/.bun` содержит только `install`), исполняемый Godot (есть только архив `/root/godot_4.7.2_linux_x86_64.zip` 78 MB и пустой каталог), psql, mysql.
- Docker 29.1.3 + plugin Compose 2.40.3; `docker.service` и `containerd.service` активны; 0 контейнеров, 0 образов. Отдельной команды `docker-compose` нет.
- Автоматизация: cron.service активен, пользовательского crontab нет, `/etc/cron.d` только `e2scrub_all`, плюс 14 systemd-таймеров.
- Тесты: pytest 9.2.0, pytest-asyncio 1.5.0. JS/TS-раннеров нет.
- Линтеры/форматтеры: ruff, mypy, black, flake8, pylint, isort, prettier, eslint, tsc, biome — все отсутствуют.
- Анализ кода: `rg` в `/root/.cache/opencode/bin/rg`, grep/find, jq 1.8.1, curl 8.18.0, wget 1.25.0. LSP в конфиге не настроены.
- БД: sqlite3 3.46.1. Файлы: `/root/text-world/data/world.db` 1,3 MB; `/root/.local/share/opencode/opencode.db` 36,7 MB + WAL 16,6 MB + SHM.
- Мониторинг/логи: есть htop, sar/sysstat, vmstat, netstat, ss, lsof, logrotate, rsyslog, journald. Нет iotop, fail2ban, Prometheus, Grafana. Лог OpenCode: `/root/.local/share/opencode/log/opencode.log` (1,66 MB); tool-output в `/root/.local/share/opencode/tool-output/`.

## 4. AI-возможности OpenCode
- `tools.opencode.models()`: 18 моделей, 5 провайдеров.
  - `opencode` (Personal / OpenCode), 11: tidepool-alpha-free, hy4-flash-free, iceman-free, longcat-2.5-free, flint-40b-free, deepseek-v4.1-flash-free, nemotron-3.5-super-vl-free, beam-free, mimo-v2.6-flash-free, nemotron-3.5-lightning-free, big-pickle.
  - `cheapllm`, 1: deepseek-v4.1-flash-1017.
  - `gemini-relay`, 1: gemini-3.5-flash-lite.
  - `google`, 1: gemma-4-31b-it.
  - `ollama`, 4: bonsai-2:9b, phi4-mini:latest, qwen2.5-coder:3b, qwen3:4b.
- CLI `opencode models` показывает 19 строк: дополнительно `google/gemma-4-26b-a4b-it` и `ollama/tw-qwen35-2b:latest`. Расхождение CLI vs API зафиксировано.
- Переменные окружения (только имена): `CHEAPLLM_API_KEY`, `GEMINI_API_KEY`, `OPENCODE`, `OPENCODE_SESSION_ID`, `OPENCODE_TERMINAL`. `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `AWS_*` — нет.
- Модель по умолчанию в конфиге не задана. Текущая модель сессии (runtime): `opencode/hy4-flash-free`. Субагенты: `opencode/nemotron-3.5-super-vl-free#high`.
- Локальные игровые модели Ollama (`tw-qwen35-08b`, `tw-qwen35-2b`) обслуживаются `text-world-ollama.service` на `127.0.0.1:11435`, но в API-каталоге OpenCode не значатся.
- Ограничений параллелизма в конфиге/API не найдено. Фактически есть 3 субагента и Code Mode `execute` для параллельных вызовов через `Promise.allSettled`.
- Контекст: сессии в SQLite `/root/.local/share/opencode/opencode.db` (WAL), логи в `opencode.log`, результаты инструментов в `tool-output/`. Конфиг discovery ищет `opencode.json(c)` и `.opencode/opencode.json(c)` по всем родительским путям; `cli.json` — отдельно для TUI.

## 5. Проект ARGO (только среда, без аудита игры)
- 4 Git-репозитория:
  - `/root/text-world/android` → `github.com/odisodisseyt-design/argo.git`, ветка `main`, коммит `2f519f0` (`0.0.7: harden campaign decoding (map danger, npc needs), bump version`), рабочее дерево чистое, 2 ветки, тегов нет.
  - `/root/game-studio` → `github.com/Mlucana/android-platformer-godot.git`, `master`, `2ef9a83`.
  - `/root/game-studio/tools/pixel-forge` → `github.com/cochranblock/pixel-forge.git`, `main`, `b68ff739`.
  - `/root/game-studio/tools/any-gpu` → `github.com/cochranblock/any-gpu.git`, `main`, `b5dafe6`.
- `/root/text-world` (где живёт сервер) не является Git-репозиторием.
- Сборка/тесты ARGO: Gradle 8.9 + JDK 17.0.21.1 + Android SDK 35; Python 3.14 + FastAPI/uvicorn/aiosqlite/httpx/pydantic/argon2/websockets; pytest 9.2.0; Ollama-модели через systemd-сервис.

## Вопросы к тебе
1. Что из перечисленного является реальным узким местом для OpenCode на 4 ядрах и 8 ГБ без GPU?
2. Как без затрат улучшить ситуацию с секретами в `opencode.json` и `service.json`?
3. Нужен ли MCP, если сейчас его нет, и если да — какой минимальный open-source сервер даст максимальную выгоду именно здесь?
4. Стоит ли добавить проектный `opencode.json` для ARGO и что в нём обязательно должно быть с учётом запрета на раскрытие внутренних данных игроку?
5. Как организовать линтеры/форматтеры и тесты, если Node.js отсутствует, а Python-инструменты не установлены?
6. Как объяснить расхождение между CLI-списком моделей (19) и API-каталогом (18)?
7. Как лучше использовать Code Mode и субагентов, чтобы не упираться в контекст и память?
