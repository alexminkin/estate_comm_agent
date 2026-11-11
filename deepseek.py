"""DeepSeek requests using only the Python standard library."""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LAST_ERROR = None


def _ask(messages, json_output=False):
    global LAST_ERROR
    LAST_ERROR = None
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not key or key == "replace_me":
        LAST_ERROR = "ключ не задан"
        return None
    body = {"model": "deepseek-flash", "thinking": {"type": "disabled"}, "messages": messages, "max_tokens": 900}
    if json_output:
        body["response_format"] = {"type": "json_object"}
    request = Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=25) as response:
            result = json.load(response)
        content = result["choices"][0]["message"]["content"]
        return content.strip() if content else None
    except HTTPError as exc:
        LAST_ERROR = f"HTTP {exc.code}"
        return None
    except (URLError, TimeoutError):
        LAST_ERROR = "нет соединения с API"
        return None
    except (ValueError, KeyError, IndexError, TypeError):
        LAST_ERROR = "некорректный ответ API"
        return None


def explain(space, ranked, category_counts):
    facts = {
        "unit": {"id": space["id"], "floor": space["floor"], "area_m2": space["area"]},
        "candidates": [{"id": item["id"], "name": item["name"], "category": item["category"], "area_min": item["min_area"], "area_max": item["max_area"]} for _, item in ranked],
        "existing_category_counts": category_counts,
        "district_signal": "К концу 2028 года в Северном районе ожидается около 12 тыс. новых жителей; это будущий сигнал, не текущий спрос.",
    }
    raw = _ask([
        {"role": "system", "content": "Ты помощник УК ТЦ «Слобода». По ТОЛЬКО данным JSON дай краткое обоснование и один вопрос для проверки каждому кандидату. Не утверждай наличие инфраструктуры, актуального спроса, ставки или контактов. Ответ строго JSON: {\"items\":[{\"id\":число,\"reason\":строка,\"risk\":строка}]}"},
        {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
    ], json_output=True)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        allowed = {item["id"] for _, item in ranked}
        return {item["id"]: (item["reason"][:350], item["risk"][:250]) for item in data["items"] if item.get("id") in allowed and isinstance(item.get("reason"), str) and isinstance(item.get("risk"), str)}
    except (ValueError, KeyError, TypeError):
        return {}


def draft(space, candidate):
    facts = {"unit": space["id"], "area_m2": space["area"], "floor": space["floor"], "candidate_name": candidate["name"], "category": candidate["category"]}
    result = _ask([
        {"role": "system", "content": "Подготовь короткое деловое сообщение от УК потенциальному арендатору. Используй только факты из JSON. Не придумывай ставку, оснащение, контакты и условия. Заверши вопросом об интересе к просмотру."},
        {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
    ])
    return result[:1500] if result else (f"Здравствуйте! В ТЦ «Слобода» доступно помещение {space['id']} площадью {space['area']} м² на {space['floor']}-м этаже. "
                                       f"Ваш формат «{candidate['category']}» может подойти. Интересно обсудить условия и просмотр помещения?")


def available():
    return bool(os.getenv("DEEPSEEK_API_KEY", "").strip() not in ("", "replace_me"))


def interpret(message, admin, context):
    """Map an ordinary message to a small, validated domain action."""
    global LAST_ERROR
    actions = ["list_units", "unit_info", "apply", "answer"]
    if admin:
        actions += ["recommend", "applications", "stats", "draft", "set_rent", "set_availability", "set_status", "add_candidate", "find_units_for_candidate"]
    system = (
        "Ты диалоговый агент ТЦ «Слобода». Понимай свободный русский текст и продолжение диалога. "
        "Верни только JSON объект с полями action, args, reply. action — один из: " + ", ".join(actions) + ". "
        "args — объект. Допустимые ключи: unit_id, candidate_id, candidate_name, name, category, "
        "min_area, max_area, target_area, floor_pref, budget, contact, rent, available, status, reason, no_unit. "
        "Поля с неизвестным значением опускай; не выдумывай их. Номера помещений только из контекста. "
        "Если человек не выбрал конкретное помещение и говорит примерную площадь, используй action=apply с target_area либо min_area/max_area, без unit_id. "
        "Не требуй номер помещения: приложение предложит варианты, а заявку можно сохранить и без выбора помещения. "
        "Если есть незавершённое действие pending, извлеки новые поля из сообщения и продолжи его; "
        "если пользователь сменил тему, выбери новое действие. При ответе на вопрос используй action=answer и reply. "
        "Не выдавай контактные данные. Не сообщай неуказанную ставку и не называй будущий рост населения текущим спросом. "
        "Действия менеджера доступны только если admin=true. При просьбе посетителя о менеджерском действии ответь, что оно доступно УК. "
        "Примеры: 'Какие есть помещения?' -> list_units; 'Что за С-034?' -> unit_info; "
        "'Хочу арендовать С-034 под студию, 200-250 м², мы Маяк' -> apply; "
        "'Подбери арендаторов для С-034' -> recommend; 'Ставка С-034 1800' -> set_rent."
    )
    raw = _ask([
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps({"message": message, "admin": admin, "context": context}, ensure_ascii=False)},
    ], json_output=True)
    if not raw:
        return None
    try:
        result = json.loads(raw)
        if result.get("action") not in actions or not isinstance(result.get("args", {}), dict):
            LAST_ERROR = "неподдерживаемый формат ответа"
            return None
        return {"action": result["action"], "args": result.get("args", {}), "reply": str(result.get("reply", ""))[:1000]}
    except (ValueError, TypeError, KeyError):
        LAST_ERROR = "некорректный JSON ответа"
        return None
