"""Проверки качества местного рассказчика: опора на факты и чистый текст."""
from server import engine, models, provider


def test_default_narrator_is_the_readable_local_model():
    spec = models.get("fast")
    assert spec.provider == "ollama"
    assert spec.narrator_model == "tw-qwen35-2b"
    assert spec.intent_model == "tw-qwen35-08b"
    assert spec.narrator_timeout >= 25
    assert 60 <= spec.narrator_max_tokens <= 160


def test_local_prompt_has_no_copyable_example():
    prompt = provider.NARRATOR_SYSTEM_LOCAL
    assert "Пример." not in prompt
    assert "ровно два коротких предложения" in prompt.lower()
    assert "на «ты»" in prompt
    assert "не придумывай" in prompt.lower()


def test_polish_prose_removes_meta_and_broken_text():
    raw = ('Хорошо, я напишу.\nТы вышел на лестничную клетку.\n'
           'Lamp lights are blinking here.')
    result = engine._polish_prose(raw, "Ничего не изменилось.")
    assert "Хорошо" not in result
    assert "lamp" not in result.lower()
    assert result.startswith("Ты вышел")


def test_grounded_prose_drops_invented_entities():
    source = ("Ты ждёшь. Время идёт, мир не останавливается. "
              "Туман поднялся от земли. Видно на несколько шагов. "
              "пасмурно, ночь, окно, чай, радио")
    invented = ("Туман стал плотнее, забирая последние лучи солнца, и ветер придушен. "
                "Собака, что вчера бросилась в дверь, сегодня стоит неподвижно, как статуя.")
    assert engine._grounded_prose(invented, source) == ""

    grounded = "Туман стелется над землёй, и окно запотело."
    assert engine._grounded_prose(grounded, source) == grounded

    # A location label "Выход: подъезд" must not ground the verb "выходит".
    fake_person = ("Туман на мгновение рассеивается, и из подъезда выходит "
                   "фигура в серой куртке.")
    assert engine._grounded_prose(fake_person, source + " Выход: подъезд") == ""


def test_grounded_prose_keeps_normal_inflections():
    source = "Гриша держит фонарь. Вода капает на асфальт. лестница, подъезд"
    sentence = "Гриша стоит на лестнице, его фонарь освещает вход, а вода капает на асфальт."
    assert engine._grounded_prose(sentence, source) == sentence


def test_fallback_scene_uses_canon_location_detail():
    results = [{"user_id": 7, "text": "Ты ждёшь. Время идёт, мир не останавливается."}]
    world_facts = ["Пошёл дождь. Он смывает следы."]
    state = {"world": {"locations": [
        {"id": "apartment", "desc": "Тесная квартира на четвёртом этаже. За окном дым, на столе — недопитый чай."},
    ]}}
    member = {"user_id": 7, "character": {"location": "apartment"}}
    prose = engine._fallback_prose(results, world_facts, state, member, step_no=1)
    assert prose.startswith("Ты ждёшь.")
    assert "За окном дым" in prose
    assert "Пошёл дождь" in prose


def test_common_present_tense_orders_do_not_need_llm():
    from server.packs import load_pack

    pack = load_pack("refuge")
    nearby = [{"id": "exit_stairwell", "name": "Выход: дверь", "aliases": ["дверь", "подъезд"]}]
    order = engine._deterministic_intent(
        "Выхожу из квартиры и спускаюсь в подъезд.", pack, nearby
    )
    assert order is not None
    assert order["action_id"] == "move"
    assert order["target"] == "exit_stairwell"
    assert order["confidence"] >= 0.9

    assert "выхожу" in pack["actions"]["move"]["verbs"]
    assert "спускаюсь" in pack["actions"]["move"]["verbs"]


async def test_verb_without_reachable_target_skips_intent_model(player, monkeypatch):
    """A known verb with no nearby target must not spend seconds on the LLM."""
    from server.packs import load_pack
    from server.provider import provider as provider_instance

    async def must_not_call(*args, **kwargs):
        raise AssertionError("intent model must not be called")

    monkeypatch.setattr(provider_instance, "complete", must_not_call)
    pack = load_pack("refuge")
    state = {"world": {"locations": []}}
    member = {"user_id": player["id"], "character": {"location": "apartment"}}
    order = await engine.parse_intent(pack, "Иду во двор", state, member)
    assert order["action_id"] == "react"
    assert order["confidence"] >= 0.9
