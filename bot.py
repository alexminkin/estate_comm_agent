"""Lightweight public Telegram bot for the Sloboda leasing demo (Python stdlib only)."""

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import deepseek
import vacancy

ROOT = Path(__file__).resolve().parent


def database_path(raw):
    path = Path(raw).expanduser()
    return str(path if path.is_absolute() else ROOT / path)


def load_env(path=ROOT / ".env"):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def keyboard(*rows):
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]}


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, payload):
        request = Request(self.base + method, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=40) as response:
                result = json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"Telegram API {method}: HTTP {exc.code}") from exc
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API {method}: {result.get('description', 'unknown error')}")
        return result.get("result")

    def send(self, chat_id, text, markup=None):
        payload = {"chat_id": chat_id, "text": str(text)[:4000]}
        if markup:
            payload["reply_markup"] = markup
        return self.call("sendMessage", payload)

    def ack(self, callback_id):
        self.call("answerCallbackQuery", {"callback_query_id": callback_id})


ROLE_NAMES = {"manager": "менеджер", "tenant": "арендатор"}


class App:
    def __init__(self, db, telegram):
        self.db, self.telegram = db, telegram
        self.sessions, self.dialog_context = vacancy.load_conversations(db)

    def admin(self, user_id):
        return vacancy.user_role(self.db, user_id) == "manager"

    def choose_role(self, chat_id, current=None):
        intro = "Выберите роль для работы с ботом." if current is None else f"Сейчас ваша роль: {ROLE_NAMES[current]}. Выберите новую роль."
        self.telegram.send(
            chat_id,
            intro + "\n\nАрендатор — ищет свободное помещение и подаёт заявку.\nМенеджер — видит заявки, подбирает кандидатов и ведёт помещения.",
            keyboard([("Я арендатор", "role:tenant")], [("Я менеджер", "role:manager")]),
        )

    def home(self, chat_id, user_id):
        role = vacancy.user_role(self.db, user_id)
        if role is None:
            self.choose_role(chat_id)
            return
        rows = [[("Свободные помещения", "units")]]
        if self.admin(user_id):
            rows += [[("Заявки посетителей", "applications")], [("Добавить кандидата", "add")], [("Итоги", "stats")]]
        rows += [[("Моя роль", "role_info"), ("Сменить роль", "role_change")]]
        self.telegram.send(chat_id, f"Ваша роль: {ROLE_NAMES[role]}.\nАгент по аренде ТЦ «Слобода» · данные на 01.10.2026\nНапишите своими словами, что хотите узнать или сделать. Кнопки ниже — подсказки.", keyboard(*rows))

    def handle(self, update):
        user_id = None
        try:
            if "callback_query" in update:
                call = update["callback_query"]
                user_id = call["from"]["id"]
                self.telegram.ack(call["id"])
                if call["message"]["chat"]["type"] != "private":
                    return
                self.callback(call["message"]["chat"]["id"], user_id, call.get("data", ""))
            elif "message" in update:
                message = update["message"]
                user_id = message["from"]["id"]
                if message["chat"]["type"] != "private":
                    return
                self.message(message["chat"]["id"], user_id, message.get("text", ""))
        finally:
            if user_id is not None:
                vacancy.save_conversation(self.db, user_id, self.sessions.get(user_id), self.dialog_context.get(user_id))

    def message(self, chat_id, user_id, text):
        text = text.strip()
        role = vacancy.user_role(self.db, user_id)
        if role is None:
            self.choose_role(chat_id)
            return
        low = text.casefold()
        if text == "/role" or any(phrase in low for phrase in ("какая у меня роль", "какая моя роль", "моя роль", "мой статус", "кто я в боте")):
            self.telegram.send(chat_id, f"Ваша роль: {ROLE_NAMES[role]}.", keyboard([("Сменить роль", "role_change")]))
            return
        if any(phrase in low for phrase in ("сменить роль", "поменять роль", "изменить роль", "поменять свой статус", "сменить свой статус", "стать менеджером", "стать арендатором", "хочу быть менеджером", "хочу быть арендатором")):
            self.sessions.pop(user_id, None)
            self.choose_role(chat_id, role)
            return
        if text in ("/start", "/menu"):
            self.sessions.pop(user_id, None)
            self.home(chat_id, user_id)
            return
        if self.handle_pending_discovery(chat_id, user_id, text):
            return
        if deepseek.available():
            self.converse(chat_id, user_id, text)
        elif user_id in self.sessions:
            self.advance(chat_id, user_id, text)
        else:
            self.simple_text(chat_id, user_id, text)

    def simple_text(self, chat_id, user_id, text, error_sent=False):
        """Small offline fallback; flexible dialogue needs a DeepSeek key."""
        low = text.casefold()
        found = re.search(r"[сc]-?0?(3[2-7])\b", low)
        unit_id = f"С-0{found.group(1)}" if found else None
        if unit_id:
            self.show_unit(chat_id, user_id, unit_id)
        elif any(word in low for word in ("свобод", "помещ", "площад")):
            self.callback(chat_id, user_id, "units")
        elif self.admin(user_id) and "заявк" in low:
            self.admin_action(chat_id, user_id, ["applications"])
        else:
            if error_sent:
                self.telegram.send(chat_id, "Пока могу показать свободные помещения и их карточки. Попробуйте ещё раз позже.")
            else:
                hint = "AI-сервис сейчас недоступен" if deepseek.available() else "Для свободного диалога укажите DEEPSEEK_API_KEY в .env и перезапустите бота"
                self.telegram.send(chat_id, hint + ". Пока можно спросить о свободных помещениях или воспользоваться кнопками.")

    def unit_id(self, raw):
        if not isinstance(raw, str):
            return None
        match = re.fullmatch(r"[СCсc]-?0?(3[2-7])", raw.strip())
        return f"С-0{match.group(1)}" if match else None

    def area_hint(self, text):
        """Read a rough size or range without requiring a fixed answer format."""
        cleaned, _ = self.contact_from_text(text)
        match = re.search(r"(?<!\d)(\d{2,5})\s*[-–—]\s*(\d{2,5})(?!\d)", cleaned)
        if match:
            low, high = map(int, match.groups())
            return {"min_area": low, "max_area": high} if 1 <= low <= high <= 100000 else None
        match = re.search(r"(?<!\d)(\d{2,5})\s*(?:м²|м2|кв\.?\s*м|метр\w*|квадрат\w*)\b|(?:около|примерно|порядка|где-то)\s*(\d{2,5})(?!\d)", cleaned.casefold())
        if match:
            area = int(match.group(1) or match.group(2))
            return {"target_area": area} if 1 <= area <= 100000 else None
        if re.fullmatch(r"\d{2,5}", cleaned.strip()):
            return {"target_area": int(cleaned.strip())}
        return None

    def suggested_unit(self, user_id, text):
        suggested = self.dialog_context.get(user_id, {}).get("suggested_units", [])
        if not suggested:
            return None
        if any(phrase in text.casefold() for phrase in ("выбери сам", "подбери сам", "самый подходящий", "любой вариант")):
            return suggested[0]
        explicit = re.search(r"[сc]-?0?(3[2-7])\b", text.casefold())
        if explicit:
            unit_id = f"С-0{explicit.group(1)}"
            return unit_id if unit_id in suggested else None
        for index, pattern in enumerate((r"\b(перв\w*|1-й|1-ый)\b", r"\b(втор\w*|2-й|2-ой)\b", r"\b(трет\w*|3-й|3-ий)\b")):
            if re.search(pattern, text.casefold()) and index < len(suggested):
                return suggested[index]
        numbers = re.findall(r"(?<!\d)\d{2,5}(?!\d)", text)
        for number in numbers:
            matches = [unit_id for unit_id in suggested if vacancy.unit(self.db, unit_id)["area"] == int(number)]
            if len(matches) == 1:
                return matches[0]
        return None

    def contact_from_text(self, text):
        """Keep contact details out of the LLM request."""
        contact_pattern = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|(?<!\w)\+?\d[\d\s()\-]{8,}\d|@[A-Za-z0-9_]{5,}"
        matches = re.findall(contact_pattern, text)
        return re.sub(contact_pattern, "[контакт указан]", text), (matches[0].strip() if matches else "")

    def pending(self, user_id):
        state = self.sessions.get(user_id)
        if not state:
            return None
        step, data = state
        if step == "dialog":
            return {"action": data["action"], "args": {k: v for k, v in data["args"].items() if k != "contact"}}
        action = "apply" if step.startswith("apply_") else "add_candidate" if step.startswith("admin_") else "set_rent" if step == "rent" else "set_status" if step == "reject" else None
        return {"action": action, "args": {k: v for k, v in data.items() if k != "contact"}} if action else None

    def handle_pending_discovery(self, chat_id, user_id, text):
        previous = self.pending(user_id)
        if previous and previous["action"] == "apply" and not previous["args"].get("unit_id") and not previous["args"].get("no_unit"):
            selected = self.suggested_unit(user_id, text)
            if selected:
                self.apply_from_text(chat_id, user_id, {**previous["args"], "unit_id": selected}, selected)
                return True
            if self.no_unit_requested(text):
                self.apply_from_text(chat_id, user_id, {**previous["args"], "no_unit": True}, None)
                return True
            area = self.area_hint(text)
            if area:
                updated = {**previous["args"], **area}
                if self.dialog_context.get(user_id, {}).get("suggested_units") and self.area_values(updated) == self.area_values(previous["args"]):
                    updated["no_unit"] = True
                    self.telegram.send(chat_id, "Понял: вы указали площадь, а конкретное помещение пока не выбрали. Сохраню общую заявку — менеджер предложит вариант.")
                self.apply_from_text(chat_id, user_id, updated, None)
                return True
        return False

    def converse(self, chat_id, user_id, text):
        safe_text, local_contact = self.contact_from_text(text)
        context = dict(self.dialog_context.get(user_id, {}))
        context["pending"] = self.pending(user_id)
        context["units"] = [{"id": row["id"], "area": row["area"], "floor": row["floor"], "rent": row["asking_rent"]} for row in vacancy.available_units(self.db)]
        context["market_note"] = "На 01.10.2026 у Слободы 6 свободных блоков, всего 1392 м². К концу 2028 года в Северном районе ожидают около 12 тыс. новых жителей."
        context["tenant_categories"] = {row[0]: row[1] for row in self.db.execute("SELECT category, COUNT(*) FROM tenants GROUP BY category")}
        if self.admin(user_id):
            context["candidates"] = [{"id": row["id"], "name": row["name"], "category": row["category"]} for row in self.db.execute("SELECT id, name, category FROM candidates ORDER BY id DESC LIMIT 30")]
        decision = deepseek.interpret(safe_text, self.admin(user_id), context)
        if decision is None:
            self.telegram.send(chat_id, f"Не удалось получить ответ DeepSeek: {deepseek.LAST_ERROR or 'неизвестная ошибка'}. Доступны базовые функции бота.")
            self.simple_text(chat_id, user_id, text, error_sent=True)
            return
        action, args = decision["action"], decision["args"]
        if action == "apply" and self.no_unit_requested(text):
            args["no_unit"] = True
        if local_contact:
            args["contact"] = local_contact
        previous = self.pending(user_id)
        if previous and action == previous["action"]:
            args = {**previous["args"], **{k: v for k, v in args.items() if v is not None}}
            stored = self.sessions[user_id][1]
            stored_args = stored.get("args", stored)
            if "contact" in stored_args and "contact" not in args:
                args["contact"] = stored_args["contact"]
        elif previous:
            self.sessions.pop(user_id, None)
        self.perform(chat_id, user_id, action, args, decision["reply"])

    def ask_missing(self, chat_id, user_id, action, args, fields):
        self.sessions[user_id] = ("dialog", {"action": action, "args": args})
        prompts = {
            "unit_id": "Какое помещение вас интересует? Укажите номер, например С-034.",
            "name": "Как называется ваша компания или проект?",
            "category": "Чем занимается ваш бизнес?",
            "min_area": "Какая площадь вам подходит? Можно написать примерно или диапазон, например 200–250 м².",
            "max_area": "Какая площадь вам подходит? Можно написать примерно или диапазон, например 200–250 м².",
            "candidate_id": "О каком кандидате речь? Напишите название или номер из списка.",
            "rent": "Какую ставку в ₽/м²/мес установить?",
            "available": "Помещение стало свободным или занятым?",
            "status": "Какой статус записать: связались, интерес, просмотр или отказ?",
        }
        self.telegram.send(chat_id, prompts.get(fields[0], "Уточните, пожалуйста, детали."))

    def resolve_candidate(self, args):
        raw_id = args.get("candidate_id")
        try:
            if raw_id is not None:
                item = vacancy.candidate(self.db, int(raw_id))
                if item:
                    return item
        except (ValueError, TypeError):
            pass
        name = str(args.get("candidate_name", "")).casefold().strip()
        if not name:
            return None
        rows = self.db.execute("SELECT * FROM candidates").fetchall()
        matches = [row for row in rows if name in row["name"].casefold()]
        return matches[0] if len(matches) == 1 else None

    def area_values(self, args):
        try:
            target = args.get("target_area")
            minimum = args.get("min_area", target)
            maximum = args.get("max_area", target)
            if minimum is None or maximum is None:
                return None
            minimum, maximum = int(minimum), int(maximum)
            return (minimum, maximum) if 1 <= minimum <= maximum <= 100000 else None
        except (ValueError, TypeError):
            return None

    def no_unit_requested(self, text):
        low = text.casefold()
        return low.strip() in {"не знаю", "пока не знаю", "не определился", "не определилась"} or any(phrase in low for phrase in ("не знаю какое", "не знаю какой", "не знаю помещение", "без выбора помещения", "без конкретного помещения", "пока не выбрал", "пока не выбрала", "не хочу выбирать"))

    def apply_from_text(self, chat_id, user_id, args, unit_id):
        if args.get("no_unit"):
            missing = []
            if not self.area_values(args):
                missing.append("min_area")
            if not str(args.get("name", "")).strip():
                missing.append("name")
            if not str(args.get("category", "")).strip():
                missing.append("category")
            if missing:
                self.ask_missing(chat_id, user_id, "apply", args, missing)
                return
            minimum, maximum = self.area_values(args)
            try:
                vacancy.create_general_inquiry(self.db, user_id, str(args["name"]), str(args["category"]), minimum, maximum, str(args.get("contact") or ""))
            except ValueError as exc:
                self.telegram.send(chat_id, f"Заявка не сохранена: {exc}")
            else:
                self.telegram.send(chat_id, f"Заявка без выбранного помещения сохранена. Менеджер увидит, что проекту «{args['name']}» нужно {minimum}–{maximum} м², и предложит вариант.")
            self.sessions.pop(user_id, None)
            self.dialog_context.get(user_id, {}).pop("suggested_units", None)
            return
        if not unit_id:
            area = self.area_values(args)
            if area:
                midpoint = (area[0] + area[1]) / 2
                rows = vacancy.available_units(self.db)
                rows = sorted(rows, key=lambda row: (max(area[0] - row["area"], row["area"] - area[1], 0), abs(row["area"] - midpoint), row["id"]))[:3]
                if rows:
                    self.dialog_context.setdefault(user_id, {})["suggested_units"] = [row["id"] for row in rows]
                    self.sessions[user_id] = ("dialog", {"action": "apply", "args": args})
                    options = "\n".join(f"{index}. {row['id']} — {row['area']} м², {row['floor']} этаж" for index, row in enumerate(rows, 1))
                    self.telegram.send(chat_id, "По вашей площади ближе всего:\n" + options + "\nМожно ответить «первый», указать площадь или номер. Если пока не хотите выбирать, так и напишите — сохраню заявку без помещения.")
                else:
                    self.telegram.send(chat_id, "Сейчас свободных помещений нет. Можете уточнить пожелания позже.")
                return
            self.ask_missing(chat_id, user_id, "apply", args, ["min_area"])
            return
        space = vacancy.unit(self.db, unit_id)
        if not space or not space["available"]:
            self.telegram.send(chat_id, "Это помещение сейчас недоступно. Могу показать другие свободные варианты.")
            self.sessions.pop(user_id, None)
            return
        missing = []
        if not str(args.get("name", "")).strip():
            missing.append("name")
        if not str(args.get("category", "")).strip():
            missing.append("category")
        if not self.area_values(args):
            missing.append("min_area")
        if missing:
            self.ask_missing(chat_id, user_id, "apply", {**args, "unit_id": unit_id}, missing)
            return
        minimum, maximum = self.area_values(args)
        try:
            vacancy.create_application(self.db, unit_id, user_id, str(args["name"]), str(args["category"]), minimum, maximum, str(args.get("contact") or ""))
        except ValueError as exc:
            self.telegram.send(chat_id, f"Заявка не сохранена: {exc}")
        else:
            self.telegram.send(chat_id, f"Заявка на {unit_id} сохранена. Менеджер УК увидит ваш проект «{args['name']}» и сможет связаться с вами.")
        self.sessions.pop(user_id, None)
        self.dialog_context.get(user_id, {}).pop("suggested_units", None)

    def set_rent_from_text(self, chat_id, user_id, args, unit_id):
        if not unit_id or args.get("rent") is None:
            self.ask_missing(chat_id, user_id, "set_rent", {**args, "unit_id": unit_id}, ["unit_id" if not unit_id else "rent"])
            return
        try:
            rent = int(args["rent"])
            if not 0 <= rent <= 1000000:
                raise ValueError
        except (ValueError, TypeError):
            self.telegram.send(chat_id, "Ставка должна быть числом от 0 до 1 000 000 ₽/м²/мес.")
            return
        with self.db:
            self.db.execute("UPDATE units SET asking_rent=? WHERE id=?", (rent or None, unit_id))
        self.sessions.pop(user_id, None)
        self.telegram.send(chat_id, f"Ставка {unit_id} {'удалена' if rent == 0 else f'установлена: {rent} ₽/м²/мес'}.")

    def add_candidate_from_text(self, chat_id, user_id, args):
        missing = []
        if not str(args.get("name", "")).strip():
            missing.append("name")
        if not str(args.get("category", "")).strip():
            missing.append("category")
        if not self.area_values(args):
            missing.append("min_area")
        if missing:
            self.ask_missing(chat_id, user_id, "add_candidate", args, missing)
            return
        minimum, maximum = self.area_values(args)
        try:
            floor = args.get("floor_pref")
            floor = None if floor in (None, 0, "0", "любой") else int(floor)
            budget = args.get("budget")
            budget = None if budget in (None, "", 0, "0") else int(budget)
            candidate_id = vacancy.add_candidate(self.db, str(args["name"]), str(args["category"]), minimum, maximum, floor, budget, str(args.get("contact") or ""))
        except (ValueError, TypeError) as exc:
            self.telegram.send(chat_id, f"Кандидат не сохранён: {exc}")
            return
        self.sessions.pop(user_id, None)
        self.telegram.send(chat_id, f"Кандидат #{candidate_id} «{args['name']}» добавлен.")

    def perform(self, chat_id, user_id, action, args, reply):
        if action not in {"list_units", "unit_info", "apply", "answer"} and not self.admin(user_id):
            self.telegram.send(chat_id, "Это действие доступно менеджеру УК.")
            return
        raw_unit = args.get("unit_id") or (None if action == "apply" else self.dialog_context.get(user_id, {}).get("last_unit_id"))
        unit_id = self.unit_id(raw_unit)
        if unit_id:
            self.dialog_context.setdefault(user_id, {})["last_unit_id"] = unit_id
        if action == "list_units":
            self.sessions.pop(user_id, None)
            rows = vacancy.available_units(self.db)
            try:
                target = int(args.get("target_area"))
                rows = sorted(rows, key=lambda row: abs(row["area"] - target))
            except (ValueError, TypeError):
                pass
            if rows:
                body = "\n".join(f"{row['id']} — {row['area']} м², {row['floor']} этаж" for row in rows)
                self.telegram.send(chat_id, "Свободные помещения ТЦ «Слобода» (срез 01.10.2026):\n" + body + "\nМожете спросить про любое помещение или сразу описать свой бизнес.")
            else:
                self.telegram.send(chat_id, "Свободных помещений сейчас нет.")
        elif action == "unit_info":
            if not unit_id:
                self.ask_missing(chat_id, user_id, action, args, ["unit_id"])
                return
            self.sessions.pop(user_id, None)
            self.show_unit(chat_id, user_id, unit_id)
        elif action == "apply":
            self.apply_from_text(chat_id, user_id, args, unit_id)
        elif action == "answer":
            self.sessions.pop(user_id, None)
            self.telegram.send(chat_id, reply or "Расскажите, какая площадь или формат бизнеса вас интересуют, и я помогу с помещением.")
        elif action == "recommend":
            if not unit_id:
                self.ask_missing(chat_id, user_id, action, args, ["unit_id"])
            else:
                self.sessions.pop(user_id, None)
                self.show_shortlist(chat_id, unit_id)
        elif action == "applications":
            self.sessions.pop(user_id, None)
            self.admin_action(chat_id, user_id, ["applications"])
        elif action == "stats":
            self.sessions.pop(user_id, None)
            self.admin_action(chat_id, user_id, ["stats"])
        elif action == "draft":
            item = self.resolve_candidate(args)
            if not unit_id:
                self.ask_missing(chat_id, user_id, action, args, ["unit_id"])
            elif not item:
                self.ask_missing(chat_id, user_id, action, args, ["candidate_id"])
            else:
                self.sessions.pop(user_id, None)
                self.dialog_context.setdefault(user_id, {})["last_candidate_id"] = item["id"]
                self.admin_action(chat_id, user_id, ["draft", unit_id, str(item["id"])])
        elif action == "set_rent":
            self.set_rent_from_text(chat_id, user_id, args, unit_id)
        elif action == "set_availability":
            if not unit_id or not isinstance(args.get("available"), bool):
                self.ask_missing(chat_id, user_id, action, args, ["unit_id" if not unit_id else "available"])
            else:
                with self.db:
                    self.db.execute("UPDATE units SET available=? WHERE id=?", (int(args["available"]), unit_id))
                self.sessions.pop(user_id, None)
                self.telegram.send(chat_id, f"Статус {unit_id} обновлён: {'свободно' if args['available'] else 'занято'}.")
        elif action == "set_status":
            item = self.resolve_candidate(args)
            raw_status = args.get("status")
            status = {"заинтересован": "интерес", "заинтересованы": "интерес", "встреча": "просмотр", "отказался": "отказ", "контакт": "связались"}.get(raw_status, raw_status) if isinstance(raw_status, str) else None
            if not unit_id or not item or status not in vacancy.STATUSES:
                missing = "unit_id" if not unit_id else "candidate_id" if not item else "status"
                self.ask_missing(chat_id, user_id, action, args, [missing])
            else:
                vacancy.set_status(self.db, unit_id, item["id"], status, str(args.get("reason", "")))
                self.sessions.pop(user_id, None)
                self.telegram.send(chat_id, f"Для {unit_id} и {item['name']} записан статус: {status}.")
        elif action == "add_candidate":
            self.add_candidate_from_text(chat_id, user_id, args)
        elif action == "find_units_for_candidate":
            item = self.resolve_candidate(args)
            if not item:
                self.ask_missing(chat_id, user_id, action, args, ["candidate_id"])
            else:
                self.sessions.pop(user_id, None)
                self.show_spaces_for_candidate(chat_id, item["id"])

    def callback(self, chat_id, user_id, data):
        parts = data.split(":")
        action = parts[0]
        if action == "role" and len(parts) == 2 and parts[1] in ROLE_NAMES:
            vacancy.set_user_role(self.db, user_id, parts[1])
            self.sessions.pop(user_id, None)
            self.dialog_context.pop(user_id, None)
            self.home(chat_id, user_id)
            return
        role = vacancy.user_role(self.db, user_id)
        if role is None:
            self.choose_role(chat_id)
            return
        if action == "role_info":
            self.telegram.send(chat_id, f"Ваша роль: {ROLE_NAMES[role]}.", keyboard([("Сменить роль", "role_change")]))
            return
        if action == "role_change":
            self.sessions.pop(user_id, None)
            self.choose_role(chat_id, role)
            return
        if action == "home":
            self.sessions.pop(user_id, None)
            self.home(chat_id, user_id)
        elif action == "units":
            rows = vacancy.available_units(self.db)
            if not rows:
                self.telegram.send(chat_id, "Свободных помещений нет.")
            else:
                choices = [[(f"{row['id']} · {row['area']} м² · {row['floor']} этаж", f"unit:{row['id']}")] for row in rows]
                self.telegram.send(chat_id, "Свободные помещения · срез 01.10.2026:", keyboard(*choices, [("Меню", "home")]))
        elif action == "unit" and len(parts) == 2:
            self.show_unit(chat_id, user_id, parts[1])
        elif action == "apply" and len(parts) == 2:
            space = vacancy.unit(self.db, parts[1])
            if not space or not space["available"]:
                self.telegram.send(chat_id, "Помещение уже недоступно.")
            else:
                self.sessions[user_id] = ("apply_name", {"unit_id": parts[1]})
                self.telegram.send(chat_id, f"Заявка на помещение {parts[1]}. Как назвать вашу компанию или проект? /menu — отмена.")
        elif action in {"toggle", "rent", "pick", "candidate", "draft", "status", "reject", "add", "applications", "stats", "find_units"}:
            if not self.admin(user_id):
                self.telegram.send(chat_id, "Это действие доступно менеджеру УК.")
                return
            self.admin_action(chat_id, user_id, parts)

    def show_unit(self, chat_id, user_id, unit_id):
        space = vacancy.unit(self.db, unit_id)
        if not space:
            self.telegram.send(chat_id, "Помещение не найдено.")
            return
        rent = f"{space['asking_rent']} ₽/м²/мес" if space["asking_rent"] else "ставка требует уточнения"
        status = "свободно" if space["available"] else "занято"
        rows = []
        if self.admin(user_id):
            rows = [[("Подобрать кандидатов", f"pick:{unit_id}")], [("Указать ставку", f"rent:{unit_id}")], [("Изменить статус", f"toggle:{unit_id}")]]
        elif space["available"]:
            rows = [[("Подать заявку", f"apply:{unit_id}")]]
        self.telegram.send(chat_id, f"{unit_id} · {space['area']} м² · {space['floor']} этаж\nСтатус: {status}\n{rent}\nСрез: {space['source_date']}", keyboard(*rows, [("К списку", "units")]))

    def admin_action(self, chat_id, user_id, parts):
        action = parts[0]
        if action == "toggle" and len(parts) == 2:
            space = vacancy.unit(self.db, parts[1])
            if space:
                with self.db:
                    self.db.execute("UPDATE units SET available=? WHERE id=?", (0 if space["available"] else 1, parts[1]))
                self.show_unit(chat_id, user_id, parts[1])
        elif action == "rent" and len(parts) == 2:
            self.sessions[user_id] = ("rent", {"unit_id": parts[1]})
            self.telegram.send(chat_id, "Введите ставку ₽/м²/мес или 0, чтобы удалить её. /menu — отмена.")
        elif action == "pick" and len(parts) == 2:
            self.show_shortlist(chat_id, parts[1])
        elif action == "candidate" and len(parts) == 3:
            self.show_candidate(chat_id, parts[1], int(parts[2]))
        elif action == "draft" and len(parts) == 3:
            space, item = vacancy.unit(self.db, parts[1]), vacancy.candidate(self.db, int(parts[2]))
            if space and item:
                self.telegram.send(chat_id, "Черновик для проверки менеджером:\n\n" + deepseek.draft(space, item))
        elif action == "status" and len(parts) == 4:
            vacancy.set_status(self.db, parts[1], int(parts[2]), parts[3])
            self.telegram.send(chat_id, f"Статус сохранён: {parts[3]}.")
        elif action == "reject" and len(parts) == 3:
            self.sessions[user_id] = ("reject", {"unit_id": parts[1], "candidate_id": int(parts[2])})
            self.telegram.send(chat_id, "Напишите короткую причину отказа. /menu — отмена.")
        elif action == "add":
            self.sessions[user_id] = ("admin_name", {})
            self.telegram.send(chat_id, "Название кандидата? /menu — отмена.")
        elif action == "applications":
            rows = vacancy.applications(self.db)
            self.telegram.send(chat_id, f"Заявок: {len(rows)}. Последние 10:")
            for row in rows[:10]:
                unit_label = row["unit_id"] or "помещение не выбрано"
                action_row = [("Карточка и действия", f"candidate:{row['unit_id']}:{row['candidate_id']}")] if row["unit_id"] else [("Подобрать помещения", f"find_units:{row['candidate_id']}")]
                self.telegram.send(chat_id, f"#{row['id']} · {unit_label} · {row['created_at']}\n{row['name']} · {row['category']} · {row['min_area']}–{row['max_area']} м²\nКонтакт: {row['contact']}", keyboard(action_row))
        elif action == "find_units" and len(parts) == 2:
            self.show_spaces_for_candidate(chat_id, int(parts[1]))
        elif action == "stats":
            rows = vacancy.stats(self.db)
            body = "\n".join(f"{row[0]}: {row[1]}" for row in rows) or "Пока нет откликов."
            self.telegram.send(chat_id, "Результаты подбора:\n" + body)

    def show_shortlist(self, chat_id, unit_id):
        space = vacancy.unit(self.db, unit_id)
        if not space:
            return
        ranked = vacancy.shortlist(self.db, unit_id)
        if not ranked:
            self.telegram.send(chat_id, "Подходящих кандидатов пока нет. Добавьте лид или проверьте статус помещения.")
            return
        counts = {row[0]: row[1] for row in self.db.execute("SELECT category, COUNT(*) FROM tenants GROUP BY category")}
        explanations = deepseek.explain(space, ranked, counts)
        self.telegram.send(chat_id, f"Кандидаты для {unit_id} · {space['area']} м²:")
        for score, item in ranked:
            if not self.db.execute("SELECT 1 FROM matches WHERE unit_id=? AND candidate_id=?", (unit_id, item["id"])).fetchone():
                vacancy.set_status(self.db, unit_id, item["id"], "предложен")
            reason, risk = explanations.get(item["id"], (f"Площадь {space['area']} м² входит в диапазон {item['min_area']}–{item['max_area']} м²; категория: {item['category']}.", "Уточнить инженерные требования и интерес к помещению."))
            label = " [ДЕМО]" if item["demo"] else ""
            self.telegram.send(chat_id, f"#{item['id']} · {item['name']}{label}\nОценка: {score:.0f}\nПочему: {reason}\nПроверить: {risk}", keyboard([("Карточка и действия", f"candidate:{unit_id}:{item['id']}")]))

    def show_spaces_for_candidate(self, chat_id, candidate_id):
        item = vacancy.candidate(self.db, candidate_id)
        if not item:
            self.telegram.send(chat_id, "Кандидат не найден.")
            return
        rows = vacancy.available_units(self.db)
        rows = [row for row in rows if item["floor_pref"] is None or row["floor"] == item["floor_pref"]]
        middle = (item["min_area"] + item["max_area"]) / 2
        rows.sort(key=lambda row: (max(item["min_area"] - row["area"], row["area"] - item["max_area"], 0), abs(row["area"] - middle), row["id"]))
        if not rows:
            self.telegram.send(chat_id, "Свободных помещений для этого кандидата нет.")
            return
        options = [[(f"{row['id']} · {row['area']} м²", f"candidate:{row['id']}:{candidate_id}")] for row in rows[:3]]
        self.telegram.send(chat_id, f"Ближайшие помещения для #{candidate_id} · {item['name']} ({item['min_area']}–{item['max_area']} м²):", keyboard(*options))

    def show_candidate(self, chat_id, unit_id, candidate_id):
        item = vacancy.candidate(self.db, candidate_id)
        if not item:
            return
        label = " [ДЕМО]" if item["demo"] else ""
        rows = [[("Черновик предложения", f"draft:{unit_id}:{candidate_id}")],
                [("Одобрить", f"status:{unit_id}:{candidate_id}:одобрен"), ("Связались", f"status:{unit_id}:{candidate_id}:связались")],
                [("Интерес", f"status:{unit_id}:{candidate_id}:интерес"), ("Просмотр", f"status:{unit_id}:{candidate_id}:просмотр")],
                [("Отказ + причина", f"reject:{unit_id}:{candidate_id}")]]
        self.telegram.send(chat_id, f"#{item['id']} · {item['name']}{label}\nКатегория: {item['category']}\nНужная площадь: {item['min_area']}–{item['max_area']} м²\nКонтакт: {item['contact'] or 'не указан'}", keyboard(*rows))

    def advance(self, chat_id, user_id, text):
        step, data = self.sessions[user_id]
        if step.startswith("admin_") or step in {"rent", "reject"}:
            if not self.admin(user_id):
                self.sessions.pop(user_id, None)
                self.telegram.send(chat_id, "Это действие доступно менеджеру УК.")
                return
        if step == "rent":
            amount = self.number(chat_id, text, allow_zero=True)
            if amount is None:
                return
            with self.db:
                self.db.execute("UPDATE units SET asking_rent=? WHERE id=?", (amount or None, data["unit_id"]))
            self.sessions.pop(user_id)
            self.telegram.send(chat_id, "Ставка сохранена.")
            return
        if step == "reject":
            vacancy.set_status(self.db, data["unit_id"], data["candidate_id"], "отказ", text)
            self.sessions.pop(user_id)
            self.telegram.send(chat_id, "Отказ и причина сохранены.")
            return
        if step in {"apply_name", "admin_name"}:
            data["name"] = text
            next_step, prompt = step.replace("name", "category"), "Какой формат бизнеса?"
        elif step in {"apply_category", "admin_category"}:
            data["category"] = text
            next_step, prompt = step.replace("category", "area"), "Диапазон площади в м², например 150-250?"
        elif step in {"apply_area", "admin_area"}:
            try:
                minimum, maximum = (int(part.strip()) for part in text.replace("–", "-").split("-"))
                if not 1 <= minimum <= maximum <= 100000:
                    raise ValueError
            except ValueError:
                self.telegram.send(chat_id, "Введите два числа, например 150-250.")
                return
            data.update(min_area=minimum, max_area=maximum)
            if step == "apply_area":
                next_step, prompt = "apply_contact", "Оставьте телефон или @username. Отправьте «-», чтобы УК увидела ваш Telegram ID."
            else:
                next_step, prompt = "admin_floor", "Предпочтительный этаж: 1, 2 или 0 (любой)?"
        elif step == "admin_floor":
            if text not in {"0", "1", "2"}:
                self.telegram.send(chat_id, "Введите 1, 2 или 0.")
                return
            data["floor_pref"] = None if text == "0" else int(text)
            next_step, prompt = "admin_budget", "Максимальная ставка ₽/м²/мес или 0, если неизвестна?"
        elif step == "admin_budget":
            amount = self.number(chat_id, text, allow_zero=True)
            if amount is None:
                return
            data["budget"] = amount or None
            next_step, prompt = "admin_contact", "Контакт кандидата или «-», если неизвестен?"
        elif step == "apply_contact":
            try:
                vacancy.create_application(self.db, data["unit_id"], user_id, data["name"], data["category"], data["min_area"], data["max_area"], "" if text == "-" else text)
            except ValueError as exc:
                self.telegram.send(chat_id, f"Заявка не сохранена: {exc}")
            else:
                self.telegram.send(chat_id, "Заявка сохранена и доступна менеджеру УК. Спасибо!")
            self.sessions.pop(user_id)
            return
        elif step == "admin_contact":
            try:
                candidate_id = vacancy.add_candidate(self.db, data["name"], data["category"], data["min_area"], data["max_area"], data["floor_pref"], data["budget"], "" if text == "-" else text)
            except ValueError as exc:
                self.telegram.send(chat_id, f"Не удалось сохранить: {exc}")
            else:
                self.telegram.send(chat_id, f"Кандидат #{candidate_id} добавлен.")
            self.sessions.pop(user_id)
            return
        else:
            self.sessions.pop(user_id)
            self.home(chat_id, user_id)
            return
        self.sessions[user_id] = (next_step, data)
        self.telegram.send(chat_id, prompt)

    def number(self, chat_id, text, allow_zero=False):
        try:
            value = int(text)
            if not (0 if allow_zero else 1) <= value <= 1000000:
                raise ValueError
            return value
        except ValueError:
            self.telegram.send(chat_id, "Введите число от 0 до 1000000.")
            return None


def main():
    load_env()
    source = ROOT / "knowledge.md"
    if "--check" in sys.argv:
        db = vacancy.connect(":memory:")
        vacancy.initialize(db, source)
        print(f"Python: {sys.version.split()[0]}; свободных помещений: {len(vacancy.available_units(db))}; внешних пакетов: 0")
        print("Telegram токен:", "задан" if os.getenv("TELEGRAM_BOT_TOKEN") not in (None, "", "replace_me") else "нужно указать")
        print("DeepSeek ключ:", "задан" if os.getenv("DEEPSEEK_API_KEY") not in (None, "", "replace_me") else "не задан (доступен шаблонный режим)")
        return
    if "--check-api" in sys.argv:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token or token == "replace_me":
            print("Telegram: токен не задан")
        else:
            try:
                Telegram(token).call("getMe", {})
                print("Telegram: соединение успешно")
            except (URLError, TimeoutError, RuntimeError) as exc:
                print(f"Telegram: соединение не удалось ({type(exc).__name__})")
        result = deepseek.interpret("Какие помещения свободны?", False, {"units": [{"id": "С-034", "area": 223, "floor": 2}]})
        print("DeepSeek:", "соединение успешно" if result else f"не удалось ({deepseek.LAST_ERROR or 'неизвестная ошибка'})")
        return
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token or token == "replace_me":
        raise SystemExit("Укажите TELEGRAM_BOT_TOKEN в .env")
    db = vacancy.connect(database_path(os.getenv("DATABASE_PATH", "vacancy.sqlite3")))
    vacancy.initialize(db, source)
    telegram = Telegram(token)
    telegram.call("getMe", {})
    telegram.call("deleteWebhook", {})
    app = App(db, telegram)
    print("Бот запущен. Остановить: Ctrl+C", flush=True)
    offset = vacancy.poll_offset(db)
    try:
        while True:
            try:
                updates = telegram.call("getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]})
                for update in updates:
                    try:
                        app.handle(update)
                    except Exception as exc:
                        print(f"Ошибка обработки сообщения: {type(exc).__name__}: {exc}", file=sys.stderr)
                    offset = update["update_id"] + 1
                    vacancy.save_poll_offset(db, offset)
            except (URLError, TimeoutError, RuntimeError) as exc:
                print(f"Сетевое соединение: {type(exc).__name__}. Повтор через 3 с.", file=sys.stderr)
                time.sleep(3)
    except KeyboardInterrupt:
        print("Бот остановлен.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
