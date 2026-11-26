Ты — старший ревьюер игрового бэкенда. Ниже — факты, проверенные по исходному коду сервера текстовой RPG «Арго» (FastAPI + SQLite/aiosqlite, версия 0.0.8). Задача: подтвердить или опровергнуть каждый вывод, оценить Severity и предложить минимальные исправления. Ничего не меняй. Отвечай по-русски, без выдумывания: если данных нет — скажи.

Ограничение: сервер — единственный источник правды; игрок не должен видеть промты, reason_code, margin, технические ID и служебные поля; пошаговый кооп; модель предлагает, код решает.

## Ключевые файлы
- `server/engine.py`: submit_order (365), _load_idempotent (344), _resolve_step (874), _narrate_one (844), _save_state (234), choose_class (1060), set_campaign_model (1118), delete_campaign (1138), _class_selection_view (~1030).
- `server/main.py`: lifespan (22), _campaign_state (186), leave_campaign (359), turn (384), allocate (429), buy (476), branches (562), accept_invite (681).
- `server/resolver.py`: preview (~241), resolve (~160).
- `server/locks.py`: campaign_lock (flock на файл кампании, кросспроцессный, снимается ОС при падении процесса).
- `server/limits.py`, `server/migrations.py`, `server/realtime.py`, `server/dto.py`, `server/config.py` (`resolution_stale_seconds` — 49).
- `server/packs/refuge/actions.json` (эффекты), `/tmp/argo-starter/ARGO/docs/API_INTEGRATION.md` (ожидания нового клиента).

---

## 1. Может ли один игровой ход обработаться дважды?

Факты:
- `submit_order` хранит Idempotency-Key в `turn_idempotency` в той же транзакции, что и приказ и `pending_json`; повтор с тем же ключом возвращает сохранённый ответ (`_load_idempotent` вызывается и до, и внутри лока). Ошибки не кэшируются. `orders` имеет PK `(campaign_id, step_no, user_id)` — один приказ на игрока на шаг. Это защищает от повтора одного и того же приказа.
- НО `_resolve_step` (874) не проверяет, есть ли приказы на текущем шаге, и не проверяет фазу: он читает `campaign.step_no`, берёт `orders` за этот шаг и в любом случае запускает worldsim, пишет `steps` и увеличивает `step_no`.
- `submit_order` (437–444) после освобождения лока вызывает `_resolve_step`, если `pending` пуст.
- `leave_campaign` (359–377) пишет `pending_json` без лока и тоже вызывает `_resolve_step`, если `pending` пуст и `phase == 'orders'`.

Сценарий: последний игрок сдал приказ → `pending_json=[]`; до его вызова `_resolve_step` другой игрок выходит через `leave_campaign` → тоже вызывает `_resolve_step`. Первый разрешает шаг N и ставит `step_no=N+1`; второй берёт лок уже после, видит шаг N+1, приказов нет, но всё равно пишет пустую сцену и ставит `step_no=N+2`. Итог: мир тикает дважды, шаг пропущен.

Простыми словами: сервер может дважды нажать кнопку «дальше», даже если второй раз нажимать было не за что.

Возможное исправление: сделать `_resolve_step` идемпотентным — передавать ожидаемый `step_no`, делать compare-and-set `UPDATE campaigns SET phase='resolving' WHERE id=? AND step_no=? AND phase='orders'` и проверять `rowcount==1`; требовать непустые `order_rows` и пустой `pending_json`; не вызывать resolved из `leave_campaign`, а только ставить флаг.

## 2. Могут ли потеряться изменения мира при одновременных действиях?

Да. Следующие обработчики читают состояние, меняют его в памяти и пишут обратно **вне `campaign_lock` и вне одной транзакции**:

- `main.leave_campaign` (359–377): `UPDATE campaign_members`, затем `UPDATE campaigns state_json,pending_json` отдельными `db.execute`.
- `main.accept_invite` (681–700): тот же шаблон, плюс добавление игрока в `pending_json` из устаревшей копии.
- `main.allocate` (429–453): читает персонажа через `get_context`, меняет атрибуты и пишет `character_json` — может затереть XP/эффекты, которые одновременно применяет `_resolve_step`.
- `main.buy` (476–509): проверяет `shop_stock`, списывает валюту, применяет эффекты через `_apply_effects`, затем `_save_state` — без лока. Две параллельные покупки могут обе пройти проверку остатка и «продать» один товар дважды.
- `main.branches` (rewind/switch, 562–615): `_save_state` с полным снимком без лока — может откатить весь мир во время разрешения шага.
- `main.accept_detail` меняет `ledger.status` без лока (менее критично).

`db.execute` открывает новое соединение на каждый вызов, поэтому несколько запросов внутри одного обработчика не образуют транзакцию.

Простыми словами: один игрок держит в руках старую копию мира и кладёт её поверх новой — чужие изменения исчезают.

Исправление: единая точка мутации `engine.mutate(campaign_id, fn)`, которая внутри `campaign_lock` заново читает состояние, применяет `fn` и коммитит одной транзакцией. Все обработчики выше обязаны пользоваться ею; проверки остатка/лимитов делать внутри этой же критической секции.

## 3. Все ли изменения состояния защищены блокировками?

Нет. Под `campaign_lock` находятся только:
- `engine.submit_order` (400),
- `engine._resolve_step` (880),
- `engine.choose_class` (1061),
- `engine.set_campaign_model` (1119),
- `engine.delete_campaign` (1144).

Без блокировки меняют состояние: `leave_campaign`, `accept_invite`, `allocate`, `buy`, `branches`, `accept_detail` (список выше). Кроме того, `main.turn` проверяет лимиты до входа в лок, а idempotency-запись делает внутри — это корректно, но показывает, что контракт блокировок не единый.

Простыми словами: лок есть, но им пользуются лишь пять функций из одиннадцати, которые меняют игру.

Исправление: запретить прямые `db.execute(... state_json ...)` вне `engine.mutate`; добавить тест, который параллельно дёргает `buy` и `_resolve_step` и проверяет, что остаток и инвентарь согласованы.

## 4. Что произойдёт, если сервер упадёт во время обработки хода?

Хорошее:
- `_resolve_step` (~976–990) кладёт вставку в `steps`, записи `ledger`, все `character_json` и обновление `campaigns` в ОДИН `db.transaction(immediate=True)`. Падение до коммита откатывает всё — половинчатых эффектов не будет.
- `campaign_lock` основан на `flock`: ОС снимает его при гибели процесса, гонок после рестарта не остаётся.

Плохое:
- Наррация (`_narrate_one`, 844; вызовы `provider.complete`) выполняется **до** этой транзакции, под локом. Если процесс упал на генерации прозы — в БД не изменилось ничего, но `pending_json` уже `[]`, приказы за шаг N есть.
- В `main.lifespan` (22–31) нет восстановления. Настройка `TW_RESOLUTION_STALE_SECONDS` есть в `config.py:49`, но нигде не используется.
- После падения `submit_order` отвергает всех (`user_id not in pending_json`), шаг висит. Единственный случайный триггер — `leave_campaign`.
- Если падение после коммита, но до `manager.send` — состояние шага обновлено, а `turn_final` клиенты не получили; повторной отправки нет.
- Idempotency-ответ сохранён вместе с приказом: повтор с тем же ключом вернёт `order_saved=True`, хотя разрешение шага могло не произойти.

Простыми словами: если сервер упал на генерации текста, игра замирает на шаге, из которого нет выхода.

Исправление: джоба восстановления при старте — искать кампании с `pending_json='[]'`, `phase='orders'` и приказами на текущий `step_no` и вызывать защищённый `_resolve_step`; использовать `resolution_stale_seconds`; хранить `resolution_started_at`. Плюс endpoint истории шагов, чтобы переподключившийся клиент мог дочитать пропущенную сцену.

## 5. Может ли игрок получить чужие приказы или личные игровые события?

Факты:
- Хорошее: `turn_final` отправляется персонально через `manager.send(campaign_id, uid, ...)`; `narrations` берётся по ключу `uid` — каждый видит свою прозу.
- Плохое 1: `outcome_text` строится как `" ; ".join(r["text"] for r in results)` из **всех** результатов и рассылается каждому (`_resolve_step`, ~1001). Это чужие исходы действий без фильтра по зрителю.
- Плохое 2: `manager.broadcast("class_selection", _class_selection_view(pack, state, state["members"][0]))` (1008) рассылает всем выбранный класс **первого** участника (`selected.id/name/category` из `_class_selection_view`, ~1030).
- Плохое 3: `waiting_for` в `_campaign_state` (197), в `awaiting_orders`/`orders_open` и в `turn_final` — это сырые `user_id`.
- Плохое 4: `journal` в `_campaign_state` (190–192) берёт все строки `ledger` без фильтра по `status`, отдавая технические поля `source` и `status` (включая `rejected`).
- Плохое 5: `ui_events` — общий список, рассылается всем.
- Хорошее: `steps.result_json` содержит все результаты и `state_before`, но не отдаётся игровыми эндпоинтами; `/api/admin/logs` защищён `_admin`.

Простыми словами: своя проза у каждого своя, а вот чужие результаты, чужой выбранный класс и внутренние номера игроков утекают в общие сообщения.

Исправление: персональный DTO для `turn_final` (только свой результат + публичные факты мира), убрать `selected` из broadcasts или делать per-user view, заменить `user_id` на display name, фильтровать журнал по `status='canon'` и убрать служебные поля.

## 6. Могут ли внутренние данные движка попасть игроку?

Да, подтверждено:
- `resolver.preview` (~270) возвращает `{"action_id","chance","modifiers","margin"}`. `engine.submit_order` кладёт его в ответ как `preview`, `main.turn` возвращает этот dict как есть. То есть `margin` и `modifiers` — ровно те поля, которые по требованиям игроку не показываются — уходят наружу.
- `_campaign_state` отдаёт `waiting_for` (ID), `journal[].source/status`, `class_selection.selected.id`, `pack_id`.
- `character_view` в `dto.py` ставит `view["id"] = member["user_id"]` — это свой ID, не чужой; здесь проблемы нет.
- `dto.py` применён только к `character_view`, `anchor_view`, `presentation_view`. `preview`, `journal`, `waiting_for` и все WS-тела идут мимо белого списка.
- `steps.result_json.results` уже почищен в `_resolve_step` (исключены `reason_code`, `capability`, `requirement`, `margin`, `modifiers`, `effects`, `applied`, `roll`) — но этот массив ни один игровой эндпоинт не отдаёт.
- `llm_logs` хранит полные промты, но доступ к ним только у оператора — утечки игроку нет.

Простыми словами: белый список DTO есть, но не у всех дверей; через ответ на приказ игрок видит внутренний расчёт шанса.

Исправление: единый `player_view()` на все исходящие ответы; убрать из `preview` поля `margin`/`modifiers` (оставить разве что качественное описание), вырезать технические поля из журнала и WS.

## 7. Надёжно ли приложение восстанавливает состояние после потери соединения?

Нет. Факты:
- `GET /api/campaign/{id}/step/status` в сервере отсутствует (поиск по `server/*.py` не нашёл ни `step/status`, ни `step_status`). Новый клиент (`docs/API_INTEGRATION.md`, строки 15–30) именно его и ожидает после reconnect.
- WS `/ws/campaign/{id}` авторизуется токеном в query, `realtime.ConnectionManager` держит комнаты в памяти, без event ID, heartbeat и replay/resume. События, пропущенные во время офлайна, безвозвратно потеряны.
- Имена событий не совпадают с ожидаемыми клиентом: сервер шлёт `awaiting_orders`, `orders_open`, `turn_final`, `class_selection`, `member_left`, `character_updated`, `rollback`; клиент ждёт `order_submitted`, `step_resolved`, `order_timeout`.
- `GET /api/campaign/{id}` отдаёт `phase`, `step_no`, `waiting_for`, но не отдаёт `deadline`, `resolved_at`, `results_ready` и не даёт истории прозы.
- Проза хранится в `steps.result_json.narrations`, но игрового эндпоинта для неё нет.

Простыми словами: приложение может переподключиться, но узнать, что пропустило, не может — сервер не умеет «дослать» прошедший ход.

Исправление: добавить `GET /api/campaign/{id}/step/status` и `GET /api/campaign/{id}/steps?cursor=...`; ввести event ID и `last_event_id` для resume; сделать алиасы для имён событий; хранить последние N событий по кампании.

---

## Вопросы к тебе
1. Какой из семи пунктов ты бы поставил в P0 и почему?
2. Достаточно ли одного `engine.mutate()` для пунктов 2 и 3 или нужны ещё изменения в `db.py`?
3. Как лучше делать анти-дабл-резолв: `phase='resolving'` + compare-and-set или отдельный `resolution_claimed` токен?
4. Где хранить историю событий для reconnect — в SQLite-таблице или в `steps`?
5. Можно ли закрыть пункт 6 одним DTO-слоем или нужны изменения в самих обработчиках?
