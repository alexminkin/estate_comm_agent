import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import bot
import vacancy


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.acked = []

    def send(self, chat_id, text, markup=None):
        self.sent.append((chat_id, text, markup))

    def ack(self, callback_id):
        self.acked.append(callback_id)


class BotFlowTest(unittest.TestCase):
    def setUp(self):
        self.db = vacancy.connect(":memory:")
        vacancy.initialize(self.db, Path(__file__).with_name("knowledge.md"))
        self.telegram = FakeTelegram()
        self.app = bot.App(self.db, self.telegram)
        vacancy.set_user_role(self.db, 100, "manager")
        vacancy.set_user_role(self.db, 200, "tenant")

    def tearDown(self):
        self.db.close()

    def test_public_application_and_admin_visibility(self):
        self.app.message(200, 200, "/role")
        self.assertIn("арендатор", self.telegram.sent[-1][1])
        self.app.message(200, 200, "/start")
        self.assertNotIn("Заявки посетителей", str(self.telegram.sent[-1][2]))
        self.app.callback(200, 200, "unit:С-034")
        self.assertIn("Подать заявку", str(self.telegram.sent[-1][2]))
        self.app.callback(200, 200, "pick:С-034")
        self.assertIn("доступно менеджеру", self.telegram.sent[-1][1])
        self.app.callback(200, 200, "apply:С-034")
        for answer in ("Студия", "Образование", "200-250", "-"):
            self.app.message(200, 200, answer)
        self.assertEqual(len(vacancy.applications(self.db)), 1)
        self.app.callback(100, 100, "applications")
        self.assertIn("Telegram ID: 200", self.telegram.sent[-1][1])

    def test_admin_can_change_unit_and_record_feedback(self):
        self.app.callback(100, 100, "rent:С-034")
        self.app.message(100, 100, "1800")
        self.assertEqual(vacancy.unit(self.db, "С-034")["asking_rent"], 1800)
        self.app.callback(100, 100, "pick:С-034")
        self.assertEqual(len(vacancy.shortlist(self.db, "С-034")), 3)
        candidate_id = vacancy.shortlist(self.db, "С-034")[0][1]["id"]
        self.app.callback(100, 100, f"reject:С-034:{candidate_id}")
        self.app.message(100, 100, "Нужна другая локация")
        row = self.db.execute("SELECT status, reason FROM matches WHERE unit_id='С-034' AND candidate_id=?", (candidate_id,)).fetchone()
        self.assertEqual(tuple(row), ("отказ", "Нужна другая локация"))

    def test_free_text_application_with_clarification(self):
        choices = [
            {"action": "apply", "args": {"unit_id": "С-034", "category": "Образование"}, "reply": ""},
            {"action": "apply", "args": {"name": "Школа Маяк", "min_area": 200, "max_area": 240}, "reply": ""},
        ]
        with patch.object(bot.deepseek, "available", return_value=True), patch.object(bot.deepseek, "interpret", side_effect=choices):
            self.app.message(200, 200, "Хочу С-034 под школу")
            self.assertIn("называется", self.telegram.sent[-1][1])
            self.app.message(200, 200, "Мы школа Маяк, нужно 200–240 метров")
        self.assertEqual(len(vacancy.applications(self.db)), 1)
        self.assertEqual(vacancy.applications(self.db)[0]["name"], "Школа Маяк")

    def test_free_text_role_guard_and_contact_redaction(self):
        seen = []
        def interpret(message, admin, context):
            seen.append((message, admin, context))
            return {"action": "set_rent", "args": {"unit_id": "С-034", "rent": 1800}, "reply": ""}
        with patch.object(bot.deepseek, "available", return_value=True), patch.object(bot.deepseek, "interpret", side_effect=interpret):
            self.app.message(200, 200, "Ставка 1800, телефон +7 999 123 45 67")
        self.assertNotIn("999 123 45 67", seen[0][0])
        self.assertIn("доступно менеджеру", self.telegram.sent[-1][1])
        self.assertIsNone(vacancy.unit(self.db, "С-034")["asking_rent"])

    def test_manager_free_text_action(self):
        decision = {"action": "set_rent", "args": {"unit_id": "C-034", "rent": 1800}, "reply": ""}
        with patch.object(bot.deepseek, "available", return_value=True), patch.object(bot.deepseek, "interpret", return_value=decision):
            self.app.message(100, 100, "Поставь ставку 1800 для C-034")
        self.assertEqual(vacancy.unit(self.db, "С-034")["asking_rent"], 1800)

    def test_contact_is_kept_locally_across_clarification(self):
        seen = []
        decisions = [
            {"action": "apply", "args": {"unit_id": "С-034", "category": "Образование"}, "reply": ""},
            {"action": "apply", "args": {"name": "Школа", "min_area": 200, "max_area": 250}, "reply": ""},
        ]
        def interpret(message, admin, context):
            seen.append((message, context))
            return decisions.pop(0)
        with patch.object(bot.deepseek, "available", return_value=True), patch.object(bot.deepseek, "interpret", side_effect=interpret):
            self.app.message(200, 200, "С-034 под школу, контакт school@example.com")
            self.app.message(200, 200, "Мы Школа, нужно 200-250 м²")
        self.assertNotIn("school@example.com", seen[0][0])
        self.assertNotIn("school@example.com", str(seen[1][1]))
        self.assertEqual(vacancy.applications(self.db)[0]["contact"], "school@example.com")

    def test_first_visit_and_role_switch(self):
        self.app.message(300, 300, "/start")
        self.assertIsNone(vacancy.user_role(self.db, 300))
        self.assertIn("Арендатор —", self.telegram.sent[-1][1])
        self.assertIn("Менеджер —", self.telegram.sent[-1][1])
        self.app.callback(300, 300, "role:tenant")
        self.assertEqual(vacancy.user_role(self.db, 300), "tenant")
        self.assertIn("Ваша роль: арендатор", self.telegram.sent[-1][1])
        self.app.message(300, 300, "Хочу стать менеджером")
        self.assertIn("Выберите новую роль", self.telegram.sent[-1][1])
        self.app.callback(300, 300, "role:manager")
        self.assertEqual(vacancy.user_role(self.db, 300), "manager")
        self.assertIn("Заявки посетителей", str(self.telegram.sent[-1][2]))
        self.app.message(300, 300, "/start")
        self.assertIn("Ваша роль: менеджер", self.telegram.sent[-1][1])
        self.app.callback(300, 300, "role:tenant")
        self.app.callback(300, 300, "applications")
        self.assertIn("доступно менеджеру", self.telegram.sent[-1][1])

    def test_approximate_area_leads_to_options_instead_of_unit_loop(self):
        first = {"action": "apply", "args": {}, "reply": ""}
        with patch.object(bot.deepseek, "available", return_value=True), patch.object(bot.deepseek, "interpret", return_value=first):
            self.app.message(200, 200, "Хочу арендовать, но пока не знаю что")
        self.assertIn("площадь", self.telegram.sent[-1][1])
        self.app.message(200, 200, "220")
        self.assertIn("По вашей площади ближе всего", self.telegram.sent[-1][1])
        self.assertIn("С-034 — 223 м²", self.telegram.sent[-1][1])
        self.app.message(200, 200, "тот на 223 м²")
        self.assertIn("называется", self.telegram.sent[-1][1])
        self.assertEqual(self.app.pending(200)["args"]["unit_id"], "С-034")

    def test_inquiry_without_chosen_unit(self):
        first = {"action": "apply", "args": {"target_area": 220}, "reply": ""}
        with patch.object(bot.deepseek, "available", return_value=True), patch.object(bot.deepseek, "interpret", return_value=first):
            self.app.message(200, 200, "Ищу помещение примерно 220 м²")
        self.assertIn("По вашей площади ближе всего", self.telegram.sent[-1][1])
        self.app.message(200, 200, "пока не знаю")
        self.assertIn("называется", self.telegram.sent[-1][1])
        second = {"action": "apply", "args": {"name": "Школа Маяк", "category": "Образование"}, "reply": ""}
        with patch.object(bot.deepseek, "available", return_value=True), patch.object(bot.deepseek, "interpret", return_value=second):
            self.app.message(200, 200, "Мы школа Маяк, обучаем языкам")
        self.assertIn("без выбранного помещения сохранена", self.telegram.sent[-1][1])
        self.assertIsNone(vacancy.applications(self.db)[0]["unit_id"])
        self.app.callback(100, 100, "applications")
        self.assertIn("помещение не выбрано", self.telegram.sent[-1][1])
        self.assertIn("Подобрать помещения", str(self.telegram.sent[-1][2]))
        candidate_id = vacancy.applications(self.db)[0]["candidate_id"]
        self.app.callback(100, 100, f"find_units:{candidate_id}")
        self.assertIn("С-034", str(self.telegram.sent[-1][2]))

    def test_repeated_area_does_not_repeat_same_question(self):
        first = {"action": "apply", "args": {"target_area": 220}, "reply": ""}
        with patch.object(bot.deepseek, "available", return_value=True), patch.object(bot.deepseek, "interpret", return_value=first):
            self.app.message(200, 200, "Нужно примерно 220 м²")
        self.assertIn("По вашей площади", self.telegram.sent[-1][1])
        self.app.message(200, 200, "220")
        self.assertIn("называется", self.telegram.sent[-1][1])
        self.assertTrue(self.app.pending(200)["args"]["no_unit"])

    def test_relative_database_path_uses_project_directory(self):
        self.assertEqual(bot.database_path("vacancy.sqlite3"), str(bot.ROOT / "vacancy.sqlite3"))

    def test_conversation_and_poll_position_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.sqlite3")
            first_db = vacancy.connect(path)
            vacancy.initialize(first_db, Path(__file__).with_name("knowledge.md"))
            vacancy.set_user_role(first_db, 200, "tenant")
            first_app = bot.App(first_db, FakeTelegram())
            decision = {"action": "apply", "args": {"target_area": 220}, "reply": ""}
            update = {"update_id": 42, "message": {"chat": {"id": 200, "type": "private"}, "from": {"id": 200}, "text": "Ищу около 220 м², контакт school@example.com"}}
            with patch.object(bot.deepseek, "available", return_value=True), patch.object(bot.deepseek, "interpret", return_value=decision):
                first_app.handle(update)
            vacancy.save_poll_offset(first_db, update["update_id"] + 1)
            first_db.close()

            second_db = vacancy.connect(path)
            vacancy.initialize(second_db, Path(__file__).with_name("knowledge.md"))
            second_app = bot.App(second_db, FakeTelegram())
            self.assertEqual(vacancy.poll_offset(second_db), 43)
            self.assertEqual(second_app.pending(200)["args"]["target_area"], 220)
            self.assertEqual(second_app.sessions[200][1]["args"]["contact"], "school@example.com")
            self.assertEqual(second_app.suggested_unit(200, "первый"), "С-034")
            second_app.handle({"update_id": 43, "message": {"chat": {"id": 200, "type": "private"}, "from": {"id": 200}, "text": "первый"}})
            self.assertEqual(second_app.pending(200)["args"]["unit_id"], "С-034")
            second_db.close()


if __name__ == "__main__":
    unittest.main()
