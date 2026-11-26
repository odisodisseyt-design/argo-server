# Text World — server

Полная инструкция по запуску, переносу состояния и работе в OpenCode: `ONBOARDING.md`.

Серверная часть пошаговой текстовой RPG-платформы. Отвечает за логику, память,
resolver, логирование и раздачу «чистых» данных клиенту. Приложение — только оболочка.

## Запуск

```bash
cd /root/text-world
python3 -m uvicorn server.main:app --host 0.0.0.0 --port 8000 &
# или
bash run.sh
```

Проверка: `curl http://127.0.0.1:8000/api/health`

## Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `TW_PROVIDER` | `ollama` | `ollama` / `openai` / `gemini` |
| `TW_MODEL` | `tw-qwen35-2b` | модель для интента и рассказчика, если не выбрана catalog-модель |
| `TW_OLLAMA_BASE` | `http://127.0.0.1:11435` | адрес Ollama |
| `TW_OPENAI_BASE` | `https://openrouter.ai/api/v1` | OpenAI-совместимый API |
| `TW_OPENAI_KEY` | — | ключ |
| `TW_GEMINI_KEY` | — | ключ Gemini |
| `TW_OPERATOR_PASSWORD` | `change-me` | пароль оператора (логин `operator`) |
| `TW_MIN_CLIENT_VERSION` | `1` | минимальная версия клиента |
| `TW_PORT` | `8000` | порт |

## Структура

```
server/
  main.py        HTTP + WebSocket API
  engine.py      машина ходов: сбор приказов → RESOLVE → публикация
  resolver.py    детерминированные правила (0% / успех / зона ±1 / провал)
  provider.py    адаптеры LLM (Ollama / OpenAI / Gemini) + логирование вызовов
  db.py          SQLite (WAL), схема и доступ
  auth.py        Argon2 + долгоживущие токены
  dto.py         whitelisted DTO — игрок никогда не видит внутреннее состояние
  realtime.py    WebSocket-комнаты кампаний
  packs.py       загрузка и валидация миров
  doctor.py      offline-диагностика
  packs/demo/    минимальный тестовый мир
  packs/refuge/  мир «Арго»; lore.md, provenance.json — канон и решения кампании
data/            БД, логи, бэкапы
```

## Правила хода

1. Каждый участник сдаёт один приказ. Реакции нет до тех пор, пока не сдали все.
2. Сервер перемешивает порядок на каждом шаге.
3. `resolver.py` считает требуемое значение R и показатель персонажа C.
   `margin = C − R`: `≥ +2` — успех без броска, `≤ −2` — провал без броска,
   зона `−1..+1` — seed-бросок. Жёсткие гейты дают 0%.
4. Никаких чисел урона, критов и HP на клиенте — только состояния тела и условия.

## Безопасность

Игрок получает только поля из `dto.py`. Логи, промпты, `reason_code`, traceback
и внутренние ID хранятся в `llm_logs` / `audit_logs` и доступны только operators
через `/admin`.
