"""Persistence and cache checks for the evaluation workflow."""
import json
import os
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
                    self.assertEqual(len(evaluations.list_appeals()), 1)
                    self.assertEqual(evaluations.list_appeals()[0]["status"], "decided")
                    second_appeal = evaluations.create_appeal(first["id"],
                                                               {"criterion_id": "contact", "comment": "Second review"})
                    evaluations.decide_appeal(second_appeal["id"],
                                              {"decision": "confirm", "reason": "Confirmed"})
                    self.assertEqual(len(evaluations.list_appeals()), 2)
                    next_checklist = app.create_checklist(app.get_checklist(first["checklist_id"]))
                    app.activate_checklist(next_checklist["id"])
                    newer = evaluations.create_evaluation({"conversation_id": "test-1"})
                    self.assertGreater(newer["checklist_version"], first["checklist_version"])
                    self.assertEqual(len(evaluations.list_appeals("decided")), 2)
                    self.assertNotEqual(newer["id"], first["id"])
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
        # the fake engine answers as "test-model", the primary (first) model of this chain
        self.env_patch = patch.dict(os.environ, {"EVAL_MODELS": "test-model,backup-model"})
        self.env_patch.start()
        app._init_db()
        for cid in ("w-1", "w-2", "w-3"):
            self._conversation(cid, "Здравствуйте, это тест")

    def tearDown(self):
        self.env_patch.stop()
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

    def test_fallback_model_answers_are_not_reused(self):
        def fallback_engine(record, checklist):
            result, stats = self.fake_engine(record, checklist)
            result["model"] = "backup-model"  # the primary model failed, the fallback answered
            return result, stats

        with patch.object(evaluations, "_run_engine", side_effect=fallback_engine) as model:
            evaluations.create_evaluation({"conversation_id": "w-3"})

            def weights(ck):
                ck["criteria"][0]["weight"] = 25
            evaluations.create_evaluation({"conversation_id": "w-3", "checklist_id": self._new_version(weights)})
            self.assertEqual(model.call_count, 2)  # not reused: evaluated again

    def test_batch_job_survives_an_exception(self):
        real_create_batch = evaluations.create_batch

        def flaky_create_batch(request):
            if request["conversation_ids"] == ["w-2"]:
                raise RuntimeError("boom")
            return real_create_batch(request)

        job_id = "job-broken"
        evaluations._jobs[job_id] = {"id": job_id, "status": "running", "completed": 0, "failed": 0,
                                     "total": 3, "results": []}
        with patch.object(evaluations, "_run_engine", side_effect=self.fake_engine), \
                patch.object(evaluations, "create_batch", side_effect=flaky_create_batch):
            evaluations._run_batch_job(job_id, ["w-1", "w-2", "w-3"], None)
        job = evaluations.batch_progress(job_id)
        self.assertEqual((job["status"], job["completed"], job["failed"]), ("done", 3, 1))
        broken = next(r for r in job["results"] if r["conversation_id"] == "w-2")
        self.assertEqual(broken["status"], "error")
        self.assertIn("RuntimeError", broken["error"])
        ok = [r for r in job["results"] if r["conversation_id"] != "w-2"]
        self.assertTrue(all(r["evaluation"]["status"] == "done" for r in ok))

    def test_reuses_one_model_chain_from_export(self):
        # export_results.py --require-model stores keys with EVAL_MODELS = that one model
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

    def test_force_skips_cache_and_weight_reuse(self):
        with patch.object(evaluations, "_run_engine", side_effect=self.fake_engine) as model:
            first = evaluations.create_evaluation({"conversation_id": "w-1"})
            self.assertEqual(evaluations.create_evaluation({"conversation_id": "w-1"})["id"], first["id"])  # cache
            forced = evaluations.create_evaluation({"conversation_id": "w-1", "force": True})
            self.assertEqual(model.call_count, 2)
            self.assertNotEqual(forced["id"], first["id"])
            self.assertEqual(evaluations.create_evaluation({"conversation_id": "w-1"})["id"], forced["id"])  # newest

            def weights(ck):
                ck["criteria"][0]["weight"] = 25
            evaluations.create_evaluation({"conversation_id": "w-1", "checklist_id": self._new_version(weights),
                                           "force": True})
            self.assertEqual(model.call_count, 3)  # no weight-only reuse when forced
            evaluations.create_evaluation({"conversation_id": "w-1", "force": "yes"})
            self.assertEqual(model.call_count, 3)  # only a real true forces

    def test_decided_appeals_carry_over_on_weight_only_change(self):
        with patch.object(evaluations, "_run_engine", side_effect=self.fake_engine) as model:
            base = evaluations.create_evaluation({"conversation_id": "w-1"})
            decided = evaluations.create_appeal(base["id"], {"criterion_id": "empathy", "comment": "грубо"})
            evaluations.decide_appeal(decided["id"], {"decision": "change", "new_answer": "no", "reason": "проверено"})
            evaluations.create_appeal(base["id"], {"criterion_id": "closing", "comment": "открыта"})  # stays open

            def weights(ck):
                for c in ck["criteria"]:
                    if c["id"] == "empathy":
                        c["weight"] = 30
            weights_id = self._new_version(weights)
            new = evaluations.create_evaluation({"conversation_id": "w-1", "checklist_id": weights_id})
            self.assertEqual(model.call_count, 1)  # still no model call
            by = {c["id"]: c for c in new["criteria"]}
            self.assertEqual((by["empathy"]["original_answer"], by["empathy"]["final_answer"]), ("yes", "no"))
            self.assertEqual(by["empathy"]["appeal"]["decision"], "change")
            self.assertIsNone(by["closing"]["appeal"])  # open appeal is not carried
            with app._connect() as db:
                ck = json.loads(db.execute("SELECT checklist_json FROM checklists WHERE id = ?",
                                           (weights_id,)).fetchone()["checklist_json"])
            final = {c["id"]: c["final_answer"] for c in new["criteria"]}
            original = {c["id"]: c["original_answer"] for c in new["criteria"]}
            self.assertEqual(new["total"], score(ck, final)["total"])
            self.assertEqual(new["original_total"], score(ck, original)["total"])
            self.assertLess(new["total"], new["original_total"])
            listed = next(c for c in app.list_conversations() if c["id"] == "w-1")
            self.assertEqual(listed["total"], new["total"])  # the list shows the same total as the evaluation

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


class NormalizationOnLoadTest(unittest.TestCase):
    def test_create_conversation_normalizes_codes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, "DB_PATH", Path(tmp) / "t.sqlite3"):
            app._init_db()
            with patch("builtins.print") as printed:
                saved = app.create_conversation({"conversation_id": "n-1", "language": "kk", "channel": "chat",
                                                 "transcript": [{"speaker": "customer", "text": "Сәлем"},
                                                                {"speaker": "bot", "text": "Күте тұрыңыз"}]})
            self.assertEqual((saved["language"], saved["channel"]), ("KZ", "agent chat"))
            self.assertEqual([m["speaker"] for m in saved["transcript"]], ["client", "bot"])
            self.assertIn("нет ни одной реплики оператора", " ".join(str(c) for c in printed.call_args_list))


class ExportEndpointTest(unittest.TestCase):
    """GET /api/export: only ready evaluations by default, log tail on failure, ok/error counts on success."""

    def setUp(self):
        from server import export
        self.export = export
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "data" / "raw").mkdir(parents=True)
        (root / "data" / "raw" / "dev.json").write_text("{}", encoding="utf-8")
        self.root_patch = patch.object(export, "ROOT", root)
        self.root_patch.start()
        self.result_file = root / "results" / "dev" / "results.json"

    def tearDown(self):
        self.root_patch.stop()
        self.tmp.cleanup()

    def test_failure_returns_log_tail_and_default_is_only_cached(self):
        log = "\n".join(f"строка {i}" for i in range(1, 16))
        failed = __import__("subprocess").CompletedProcess([], 2, stdout=log, stderr="")
        with patch.object(self.export.subprocess, "run", return_value=failed) as run:
            with self.assertRaises(evaluations.HTTPException) as ctx:
                self.export.export_results(set="dev")
        self.assertIn("--only-cached", run.call_args[0][0])  # no paid evaluation by default
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("строка 15", ctx.exception.detail)
        self.assertIn("строка 6", ctx.exception.detail)
        self.assertNotIn("строка 5\n", ctx.exception.detail)  # only the last 10 lines

    def test_success_reports_counts(self):
        self.result_file.parent.mkdir(parents=True)
        self.result_file.write_text(json.dumps({"results": [{"status": "ok"}, {"status": "ok"}, {"status": "error"}]}),
                                    encoding="utf-8")
        done = __import__("subprocess").CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(self.export.subprocess, "run", return_value=done) as run:
            response = self.export.export_results(set="dev", only_cached=False)
        self.assertNotIn("--only-cached", run.call_args[0][0])
        self.assertEqual((response.headers["x-export-ok"], response.headers["x-export-error"]), ("2", "1"))


if __name__ == "__main__":
    unittest.main()
