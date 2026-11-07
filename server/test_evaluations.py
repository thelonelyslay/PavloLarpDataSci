"""Persistence and cache checks for the evaluation workflow."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.responses import JSONResponse

from server import app
from server import evaluations
from src.cqa.scoring import score


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
                    details = {c["id"]: {"explanation": "Test", "evidence": []} for c in checklist["criteria"]}
                    from server.evaluations import _compose
                    result = _compose("test-evaluation", record, "default-chat-v3", checklist,
                                      "test-model", 0.01, "done", answers, answers, details)
                    result["id"] = f"test-evaluation-{model.call_count}"
                    return result, {"seconds": 0.1, "input_tokens": 10, "output_tokens": 5}

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

    def test_engine_error_is_saved_as_error_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(app, "DB_PATH", Path(tmp) / "test.sqlite3"):
                app._init_db()
                with app._connect() as db:
                    db.execute("""INSERT INTO conversations (id, channel, language, transcript)
                        VALUES (?, ?, ?, ?)""", ("test-2", "agent chat", "RU", "[]"))
                with patch.object(evaluations, "_run_engine",
                                  side_effect=evaluations.EngineEvaluationError("Model unavailable")):
                    response = evaluations.create_evaluation({"conversation_id": "test-2"})
                self.assertIsInstance(response, JSONResponse)
                self.assertEqual(response.status_code, 502)
                self.assertIn(b'"status":"error"', response.body)


class WeightOnlyChangeTest(unittest.TestCase):
    """A checklist version that differs only in weights reuses the model's answers (no model call)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(app, "DB_PATH", Path(self.tmp.name) / "test.sqlite3")
        self.db_patch.start()
        app._init_db()
        for cid in ("w-1", "w-2", "w-3"):
            self._conversation(cid, "Здравствуйте, это тест")

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def _conversation(self, cid, text):
        with app._connect() as db:
            db.execute("""INSERT INTO conversations (id, channel, language, transcript) VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET transcript = excluded.transcript""",
                       (cid, "agent chat", "RU", json.dumps([{"speaker": "operator", "text": text, "timestamp": "00:00"}])))

    @staticmethod
    def fake_engine(record, checklist):
        # contact "no", everything else at the top: the total depends on the weights
        answers = {c["id"]: ("no" if c["id"] == "contact" else max(c["steps"]) if c["type"] == "scale"
                             else "no" if c["type"] == "flag" else "yes") for c in checklist["criteria"]}
        details = {c["id"]: {"explanation": "Test", "evidence": []} for c in checklist["criteria"]}
        result = evaluations._compose(uuid4().hex, record, "x", checklist, "test-model", 0.01, "done",
                                      answers, answers, details)
        return result, {"seconds": 0.1, "input_tokens": 10, "output_tokens": 5}

    def _new_version(self, change):
        with app._connect() as db:
            base = db.execute("SELECT * FROM checklists WHERE applies_to = 'agent chat' AND active = 1").fetchone()
        checklist = json.loads(base["checklist_json"])
        change(checklist)
        return app.create_checklist({**checklist, "id": base["id"]})["id"]

    def test_weights_reuse_rules_reevaluate(self):
        with patch.object(evaluations, "_run_engine", side_effect=self.fake_engine) as model:
            base = evaluations.create_evaluation({"conversation_id": "w-1"})
            self.assertEqual(model.call_count, 1)

            def weights(ck):
                for c in ck["criteria"]:
                    if c["id"] == "contact":
                        c["weight"] = 40
            weights_id = self._new_version(weights)
            rescored = evaluations.create_evaluation({"conversation_id": "w-1", "checklist_id": weights_id})
            self.assertEqual(model.call_count, 1)  # no model call
            self.assertEqual(rescored["cost_usd"], 0.0)
            self.assertEqual(rescored["model"], "test-model")
            self.assertEqual(rescored["checklist_id"], weights_id)
            self.assertLess(rescored["total"], base["total"])  # heavier failed criterion -> lower total
            with app._connect() as db:
                row = db.execute("SELECT * FROM evaluations WHERE id = ?", (rescored["id"],)).fetchone()
                ck = json.loads(db.execute("SELECT checklist_json FROM checklists WHERE id = ?",
                                           (weights_id,)).fetchone()["checklist_json"])
            self.assertEqual(row["cost_usd"], 0)
            self.assertEqual(rescored["total"], score(ck, json.loads(row["answers_json"]))["total"])
            # the reused result is cached like a normal one
            self.assertEqual(evaluations.create_evaluation({"conversation_id": "w-1", "checklist_id": weights_id})["id"],
                             rescored["id"])

            def rule(ck):
                ck["criteria"][0]["rules"] = "Изменённое правило"
            rule_id = self._new_version(rule)
            evaluations.create_evaluation({"conversation_id": "w-1", "checklist_id": rule_id})
            self.assertEqual(model.call_count, 2)  # changed rule -> normal evaluation

    def test_reuses_one_model_chain_from_export(self):
        # export_results.py --require-model stores keys with EVAL_MODELS = that one model
        import os
        with patch.object(evaluations, "_run_engine", side_effect=self.fake_engine) as model:
            with patch.dict(os.environ, {"EVAL_MODELS": "test-model"}):
                evaluations.create_evaluation({"conversation_id": "w-3"})
            with patch.dict(os.environ, {"EVAL_MODELS": "test-model,backup-model"}):
                def weights(ck):
                    ck["criteria"][0]["weight"] = 25
                rescored = evaluations.create_evaluation({"conversation_id": "w-3",
                                                          "checklist_id": self._new_version(weights)})
            self.assertEqual(model.call_count, 1)
            self.assertEqual(rescored["cost_usd"], 0.0)

    def test_changed_transcript_is_evaluated_again(self):
        with patch.object(evaluations, "_run_engine", side_effect=self.fake_engine) as model:
            evaluations.create_evaluation({"conversation_id": "w-2"})
            self._conversation("w-2", "Другой текст диалога")

            def weights(ck):
                ck["criteria"][0]["weight"] = 30
            evaluations.create_evaluation({"conversation_id": "w-2", "checklist_id": self._new_version(weights)})
            self.assertEqual(model.call_count, 2)

    def test_batch_job_runs_in_parallel(self):
        active, peak, lock = [0], [0], threading.Lock()

        def slow_engine(record, checklist):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.2)
            with lock:
                active[0] -= 1
            return self.fake_engine(record, checklist)

        job_id = "job-test"
        evaluations._jobs[job_id] = {"id": job_id, "status": "running", "completed": 0, "total": 3, "results": []}
        with patch.object(evaluations, "_run_engine", side_effect=slow_engine):
            evaluations._run_batch_job(job_id, ["w-1", "w-2", "w-3"], None)
        job = evaluations.batch_progress(job_id)
        self.assertEqual((job["status"], job["completed"]), ("done", 3))
        self.assertEqual(sorted(r["conversation_id"] for r in job["results"]), ["w-1", "w-2", "w-3"])
        self.assertGreater(peak[0], 1)


if __name__ == "__main__":
    unittest.main()
