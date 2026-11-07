"""Persistence and cache checks for the evaluation workflow."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import app
from server import evaluations


class EvaluationFlowTest(unittest.TestCase):
    def test_cache_and_appeal_recalculation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(app, "DB_PATH", Path(tmp) / "test.sqlite3"):
                app._init_db()
                with app._connect() as db:
                    db.execute("""INSERT INTO conversations (id, channel, language, transcript)
                        VALUES (?, ?, ?, ?)""", ("test-1", "agent chat", "RU",
                        '[{"speaker":"operator","text":"test","timestamp":"00:00"}]'))

                def fake_engine(record, checklist):
                    answers = {c["id"]: (max(c["steps"]) if c["type"] == "scale"
                                            else "no" if c["type"] == "flag" else "yes")
                               for c in checklist["criteria"]}
                    return answers, {}, "test-model", 0.1, 10, 5, 0.01, "complete"

                with patch.object(evaluations, "_run_engine", side_effect=fake_engine) as model:
                    first = evaluations.create_evaluation({"conversation_id": "test-1"})
                    self.assertEqual(evaluations.create_evaluation({"conversation_id": "test-1"})["id"], first["id"])
                    self.assertEqual(model.call_count, 1)
                    appeal = evaluations.create_appeal(first["id"],
                                                       {"criterion_id": "contact", "comment": "Review"})
                    revised = evaluations.decide_appeal(appeal["id"],
                                                        {"decision": "change", "new_answer": "no", "reason": "Review"})
                    self.assertLess(revised["total"], first["total"])
                    self.assertEqual(revised["original_total"], first["total"])
                    self.assertEqual(evaluations.get_evaluation(first["id"])["total"], revised["total"])
                    self.assertNotEqual(evaluations.create_evaluation({"conversation_id": "test-1"})["id"], first["id"])
                    self.assertEqual(model.call_count, 2)


if __name__ == "__main__":
    unittest.main()
