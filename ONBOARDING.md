# ARGO — запуск, восстановление и работа в OpenCode

Этот файл — точка входа для агента или человека, который впервые открыл проект
на другом устройстве. Прочитайте его **до** каких-либо изменений кода.

## 1. Что нужно прочитать агенту

1. `AGENTS.md` — обязательные правила проекта (роль главного агента, whitelist DTO, порядок проверок, запреты).
2. `ONBOARDING.md` — этот файл: окружение, перенос состояния, запуск.
3. `README.md` — краткая схема сервера.
4. `docs/` — исторические промпты аудита; это справка, не инструкция к действию.
5. `server/config.py` — полный список переменных окружения.
6. `tests/` — текущие проверки; новые фичи обязаны приходить с тестами.

Первый промпт в OpenCode на новой машине может быть таким:

> Прочитай AGENTS.md, ONBOARDING.md, README.md, server/config.py и тесты.
> Кратко опиши архитектуру и текущее состояние, затем жди моей задачи.
> Ничего не меняй и не пушь без явного разрешения.

## 2. Два репозитория

| Роль | Репозиторий | Что лежит |
|---|---|---|
| Сервер, источник правды | `https://github.com/odisodisseyt-design/argo-server` | этот репозиторий: `server/`, `tests/`, `docs/`, конфиги |
| Клиент, оболочка | `https://github.com/odisodisseyt-design/argo` | Expo/React Native в `android/mobile`, Kotlin-проект, `version.json`, APK-релизы |

Сервер **не** должен дублировать клиента: каталог `android/` в старом рабочем дереве
исключён через `.gitignore`.

```bash
gh auth login                 # если ключа ещё нет
git clone https://github.com/odisodisseyt-design/argo-server.git argo-server
git clone https://github.com/odisodisseyt-design/argo.git argo
```

## 3. Локальная разработка

Требуется Python 3.11+ (на боевом стенде — 3.14), SQLite, Ollama.

```bash
cd argo-server
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python3 -m compileall -q server
python3 -m pytest -q
```

Тесты используют **временную БД** и подменяют LLM (`tests/conftest.py`): внешние
сервисы для `pytest` не нужны. `pytest.ini` и тесты меняются только по отдельной просьбе.

Быстрый запуск сервера: `bash run.sh` или
`python3 -m uvicorn server.main:app --host 0.0.0.0 --port 8000`.
Проверка: `curl http://127.0.0.1:8000/api/health`.

## 4. Конфигурация и секреты

Все секреты — только в переменных окружения, в репозитории их нет. Полный список —
в `server/config.py`. Ключевое:

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `TW_DB_PATH` | `data/world.db` | рабочая БД |
| `TW_PUBLIC_BASE` | публичный домен | адрес для клиента и OTA |
| `TW_PROVIDER` | `ollama` | провайдер LLM |
| `TW_OLLAMA_BASE` | `http://127.0.0.1:11435` | адрес локальной Ollama |
| `TW_DEFAULT_MODEL_ID` | `fast` | модель по умолчанию |
| `TW_OPERATOR_PASSWORD` | — | пароль оператора, задаётся на сервере |
| `TW_CHEAPLLM_API_KEY` | — | ключ облачной модели, если используется облако |
| `TW_REQUIRE_IDEMPOTENCY` | `0` | обязательность `Idempotency-Key` |
| `TW_RESOLUTION_STALE_SECONDS` | `300` | аренда захвата шага |

Каталог моделей для игрока — `server/models.json`. Сейчас `fast` использует
`tw-qwen35-2b` как рассказчика и `tw-qwen35-08b` для интента; облачная модель — опция.

### Локальные модели

Исходники GGUF и Modelfile лежат в `models/`. На новом сервере:

```bash
# замените /root/text-world на фактический путь к клону
ollama create tw-qwen35-2b  -f models/Modelfile
ollama create tw-qwen35-08b -f models/Modelfile-08
curl http://127.0.0.1:11435/api/tags
```

Ollama хранит свои blob'ы отдельно (`ollama-models/` на боевом стенде), поэтому
при переносе нужно забрать и этот каталог — иначе модели придётся пересоздавать.

## 5. Сервисы и домен

На боевом сервере используются systemd-юниты `text-world` и `text-world-ollama`
(их точный текст можно скопировать с прежнего сервера: `/etc/systemd/system/`).
Схема: Caddy → `127.0.0.1:8000`, Ollama → `127.0.0.1:11435`.

OTA-манифест раздаётся самим сервером:

- `GET /api/updates/manifest` — требует заголовки `expo-protocol-version: 1`, `expo-platform: android`, `expo-runtime-version: 0.1.2`;
- `GET /api/updates/assets/{name}` — неизменяемые ассеты.

Каталог `data/updates/` обязателен для OTA и тоже переносится.

## 6. Перенос состояния на новый сервер — главное, ничего не потерять

**Не копируйте только код.** Всё состояние игры живёт в `data/`.

### На старом сервере

```bash
sudo systemctl stop text-world
cd /root/text-world
tar czf /tmp/argo-state.tgz \
  data/world.db data/world.db-wal data/world.db-shm \
  data/updates data/saves data/backups data/locks \
  models ollama-models
# плюс: переменные окружения, /etc/systemd/system/text-world*.service,
# конфиг Caddy, ключ gh — не в репозитории
```

БД копируется **вместе с `-wal` и `-shm`**, иначе можно потерять последние транзакции.
`data/logs` и `llm_logs` в БД можно забрать для истории, но они не нужны для работы.

### На новом сервере

1. Клонировать оба репозитория, установить зависимости.
2. Распаковать `/tmp/argo-state.tgz` в корень нового клона.
3. Восстановить переменные окружения, systemd-юниты, конфиг Caddy и DNS.
4. Запустить Ollama, затем `text-world` (сервер при старте сам вызывает `db.init_db()` и применяет недостающие миграции).
5. Проверить: `/api/health`, `/api/models`, список кампаний под бойцовым аккаунтом, OTA-манифест.
6. Только после проверки — направить домен на новый сервер.

Миграции идемпотентны и хранятся в `server/migrations.py`. Текущие версии:
`0007_resolution_fencing`, `0008_order_clarifications`. На рабочей БД их применяют
только после бэкапа и отдельного разрешения.

## 7. Что нельзя делать без согласования

- Менять правила хода (`server/resolver.py`, `gates.py`, `worldsim.py`, `catalog.py`) и DTO-whitelist (`dto.py`).
- Добавлять новые зависимости, менять модели/Ollama, OpenCode-конфигурацию.
- Делать `push`, `reset --hard`, `clean -f`, удалять данные.
- Применять миграции к боевой БД без отдельного разрешения.

Любая новая публичная ошибка или поле должна быть оправдана и не раскрывать
внутренние ID, промпты, стектрейсы и `reason_code`.

## 8. Текущее состояние P0

- Разрешение шага двухфазное: короткий захват с уникальным `resolution_id`, всё вычисление вне транзакции, финальный CAS по `campaign + step_no + phase + resolution_id`.
- Старый воркер не может зафиксировать результат после повторного захвата.
- `preview()` и `resolve()` используют общую функцию `_requirement()`.
- Непонятые приказы сохраняются в `order_clarifications`; ответ продолжает исходный текст и идемпотентен.
- Все проверки: `python3 -m compileall -q server` и `python3 -m pytest -q` (84 теста).
