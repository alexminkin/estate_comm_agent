import tempfile
import unittest
from pathlib import Path

import vacancy


SOURCE = Path(__file__).with_name("knowledge.md")


class VacancyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = vacancy.connect(str(Path(self.temp.name) / "test.sqlite3"))
        vacancy.initialize(self.db, SOURCE)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_source_snapshot(self):
        units = vacancy.available_units(self.db)
        self.assertEqual(len(units), 6)
        self.assertEqual(sum(row["area"] for row in units), 1392)
        self.assertTrue(all(row["floor"] == 2 for row in units))

    def test_candidate_feedback_persists_and_seed_is_idempotent(self):
        candidate_id = vacancy.add_candidate(self.db, "Новый арендатор", "Образование", 200, 240, 2)
        self.assertIn(candidate_id, [item["id"] for _, item in vacancy.shortlist(self.db, "С-034")])
        vacancy.set_status(self.db, "С-034", candidate_id, "интерес")
        vacancy.initialize(self.db, SOURCE)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 11)
        self.assertEqual(self.db.execute("SELECT status FROM matches WHERE unit_id='С-034' AND candidate_id=?", (candidate_id,)).fetchone()[0], "интерес")

    def test_price_and_availability_restrict_matches(self):
        with self.db:
            self.db.execute("UPDATE units SET asking_rent=2000 WHERE id='С-034'")
        candidate_id = vacancy.add_candidate(self.db, "Бюджетный", "Образование", 200, 240, 2, 1500)
        self.assertNotIn(candidate_id, [item["id"] for _, item in vacancy.shortlist(self.db, "С-034")])
        with self.db:
            self.db.execute("UPDATE units SET available=0 WHERE id='С-034'")
        self.assertEqual(vacancy.shortlist(self.db, "С-034"), [])

    def test_public_application_appears_for_manager(self):
        candidate_id = vacancy.create_application(self.db, "С-034", 12345, "Студия", "Образование", 200, 250)
        rows = vacancy.applications(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["candidate_id"], candidate_id)
        self.assertEqual(rows[0]["contact"], "Telegram ID: 12345")
        self.assertIn(candidate_id, [item["id"] for _, item in vacancy.shortlist(self.db, "С-034")])

    def test_user_role_persists_in_database(self):
        vacancy.set_user_role(self.db, 55, "tenant")
        vacancy.set_user_role(self.db, 55, "manager")
        self.assertEqual(vacancy.user_role(self.db, 55), "manager")
        self.db.close()
        self.db = vacancy.connect(str(Path(self.temp.name) / "test.sqlite3"))
        self.assertEqual(vacancy.user_role(self.db, 55), "manager")

    def test_general_inquiry_is_visible_without_unit(self):
        candidate_id = vacancy.create_general_inquiry(self.db, 12345, "Школа", "Образование", 200, 240)
        row = vacancy.applications(self.db)[0]
        self.assertIsNone(row["unit_id"])
        self.assertEqual(row["candidate_id"], candidate_id)
        self.assertEqual(row["contact"], "Telegram ID: 12345")


if __name__ == "__main__":
    unittest.main()
