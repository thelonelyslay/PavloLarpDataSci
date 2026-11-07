"""Export to the organizers' format and its validator, on fake data (no network, no real models).

Run: python3 -m unittest -v test_export_results   (the server cache test needs .venv with server deps)
"""
import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import export_results as ex
import validate_results as vr

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CHAT = json.loads((HERE / "checklist.default.json").read_text(encoding="utf-8"))
ANSWERS = {"contact": "yes", "needs": "na", "identification": "na", "consultation": 15.0, "empathy": "yes",
           "dialog_management": 7.0, "closing": "no", "gross_errors": "no"}
DETAILS = {cid: {"explanation": f"пояснение {cid}", "evidence": [{"message_index": 2, "quote": "x"}]} for cid in ANSWERS}
DIALOGS = {"c1": {"conversation_id": "c1", "channel": "agent chat", "transcript": [{"speaker": "client", "text": "t"}] * 5},
           "c2": {"conversation_id": "c2", "channel": "agent chat", "transcript": [{"speaker": "client", "text": "t"}] * 5}}


def doc(*results):
    return ex.build_document("-60HZ", ["claude-sonnet-5-5"], list(results), "x")


class ConvertTest(unittest.TestCase):
    def test_criteria_renamed_and_ordered(self):
        rec = ex.ok_record("c1", CHAT, ANSWERS, DETAILS)
        self.assertEqual([c["criterion_id"] for c in rec["criteria"]],
                         ["contact", "discovery", "identification", "consultation", "empathy", "dialogue", "closure", "critical"])
        self.assertEqual(rec["criteria"][0]["rationale"], "пояснение contact")

    def test_values_na_to_null_and_numbers(self):
        v = {c["criterion_id"]: c["value"] for c in ex.ok_record("c1", CHAT, ANSWERS, DETAILS)["criteria"]}
        self.assertEqual(v, {"contact": True, "discovery": None, "identification": None, "consultation": 15,
                             "empathy": True, "dialogue": 7, "closure": False, "critical": False})
        self.assertIsInstance(v["consultation"], int)

    def test_turns_shifted_by_one_without_repeats(self):
        ev = [{"message_index": 0}, {"message_index": 0}, {"message_index": 5}, {"message_index": -1}, {}]
        self.assertEqual(ex.evidence_turns(ev), [1, 6])

    def test_half_up_rounding(self):
        # applicable weight 80 (identification N/A), earned 70.5 -> 88.125 -> 88.13 (Python round would give 88.12)
        answers = {**ANSWERS, "needs": "yes", "closing": "yes"}
        self.assertEqual(round(88.125, 2), 88.12)
        rec = ex.ok_record("c1", CHAT, answers, DETAILS)
        self.assertEqual(rec["score"]["final_score"], 88.13)

    def test_critical_flag_does_not_zero_total(self):
        rec = ex.ok_record("c1", CHAT, {**ANSWERS, "gross_errors": "yes"}, DETAILS)
        self.assertTrue(rec["score"]["critical_flag"])
        self.assertEqual(rec["score"]["final_score"], ex.ok_record("c1", CHAT, ANSWERS, DETAILS)["score"]["final_score"])

    def test_error_record(self):
        rec = ex.error_record("c2", "превышен предел времени")
        self.assertEqual(rec, {"conversation_id": "c2", "status": "error", "criteria": [], "score": None,
                               "error": "превышен предел времени"})
        self.assertEqual(vr.validate(doc(rec), DIALOGS), [])

    def test_unmapped_criterion_is_an_error(self):
        ck = copy.deepcopy(CHAT)
        ck["criteria"][0]["id"] = "greeting_v2"
        with self.assertRaises(ex.ExportError):
            ex.ok_record("c1", ck, {**ANSWERS, "greeting_v2": "yes"}, DETAILS)

    def test_all_na_gives_null_final_score(self):
        # the standard checklist forbids N/A for contact/empathy/dialogue, so use a checklist that allows it everywhere
        ck = copy.deepcopy(CHAT)
        for c in ck["criteria"]:
            c["allow_na"] = True
        answers = {cid: "na" for cid in ANSWERS}
        answers["gross_errors"] = "no"
        rec = ex.ok_record("c1", ck, answers, DETAILS)
        self.assertIsNone(rec["score"]["final_score"])
        self.assertFalse(rec["score"]["critical_flag"])
        self.assertTrue(all(c["value"] is None for c in rec["criteria"][:-1]))

    def test_model_names_unique(self):
        d = ex.build_document("t", ["a", "b", "a", None], [], "x")
        self.assertEqual(d["model"], {"mode": "external_api", "name": "a, b"})


class ValidatorTest(unittest.TestCase):
    def setUp(self):
        self.good = doc(ex.ok_record("c1", CHAT, ANSWERS, DETAILS), ex.error_record("c2", "сбой"))

    def errs(self, mutate, expect=None):
        d = copy.deepcopy(self.good)
        mutate(d)
        return vr.validate(d, DIALOGS, expect)

    def test_good_file_passes(self):
        self.assertEqual(vr.validate(self.good, DIALOGS, 2), [])

    def test_expect_count(self):
        self.assertTrue(any("ожидалось 65" in e for e in vr.validate(self.good, DIALOGS, 65)))

    def test_duplicate_conversation_id(self):
        e = self.errs(lambda d: d["results"].append(copy.deepcopy(d["results"][0])))
        self.assertTrue(any("встречается 2 раза" in x for x in e))

    def test_missing_criterion(self):
        e = self.errs(lambda d: d["results"][0]["criteria"].pop(3))
        self.assertTrue(any("нет критерия consultation" in x for x in e))
        self.assertTrue(any("нужно ровно 8" in x for x in e))

    def test_wrong_types(self):
        def m(d):
            c = d["results"][0]["criteria"]
            c[0]["value"] = "yes"      # contact must be a boolean
            c[3]["value"] = True       # consultation must be a number, true is not a number
            c[5]["value"] = 5          # dialogue: only 0, 4, 7, 10
        e = self.errs(m)
        self.assertEqual(len([x for x in e if "допустимо только" in x or "нужно true или false" in x]), 3)

    def test_wrong_turn_shift(self):
        def m(d):
            d["results"][0]["criteria"][0]["evidence_turns"] = [0]
            d["results"][0]["criteria"][1]["evidence_turns"] = [6]   # dialog has 5 messages
        e = self.errs(m)
        self.assertTrue(any("меньше 1" in x for x in e))
        self.assertTrue(any("больше числа реплик" in x for x in e))

    def test_null_score_only_when_nothing_applicable(self):
        e = self.errs(lambda d: d["results"][0]["score"].update(final_score=None))
        self.assertTrue(any("не совпадает с пересчётом" in x for x in e))

    def test_score_and_flag_checked(self):
        def m(d):
            d["results"][0]["score"]["final_score"] = 99.0
            d["results"][0]["score"]["critical_flag"] = True
        e = self.errs(m)
        self.assertTrue(any("не совпадает с пересчётом" in x for x in e))
        self.assertTrue(any("critical_flag" in x for x in e))

    def test_error_record_rules_and_header(self):
        def m(d):
            d["results"][1]["criteria"] = [{"criterion_id": "contact"}]
            d["results"][1]["error"] = ""
            d["model"]["mode"] = "cloud"
            d["team_name"] = " "
        e = self.errs(m)
        for phrase in ("criteria должен быть пустым", "причина в поле error", "model.mode", "team_name"):
            self.assertTrue(any(phrase in x for x in e), phrase)

    def test_strict_json(self):
        good = json.dumps(self.good, ensure_ascii=False).encode("utf-8")
        self.assertEqual(vr.parse_strict(good)[1], [])
        self.assertIn("BOM", vr.parse_strict(b"\xef\xbb\xbf" + good)[1][0])
        self.assertIn("повторяется", vr.parse_strict(b'{"a": 1, "a": 2}')[1][0])
        self.assertIn("NaN", vr.parse_strict(b'{"a": NaN}')[1][0])
        self.assertIn("UTF-8", vr.parse_strict("{}".encode("utf-16"))[1][0])


class EvidenceCheckTest(unittest.TestCase):
    def test_quotes(self):
        dialogs = {"c1": {"transcript": [{"speaker": "operator", "text": "Здравствуйте, меня зовут Анна"},
                                         {"speaker": "client", "text": "Привет"},
                                         {"speaker": "operator", "text": "Откройте раздел Котировки и найдите облигации"}]}}
        details = {"contact": {"evidence": [{"message_index": 0, "quote": "меня  зовут Анна"}]},
                   "needs": {"evidence": [{"message_index": 1, "quote": "Откройте раздел"}]},
                   "consultation": {"evidence": [{"message_index": 2, "quote": "Откройте раздел … облигации"}]}}
        rep = ex.check_evidence([{"conversation_id": "c1", "details": details}], dialogs)
        self.assertEqual((rep["total"], rep["matched"]), (3, 1))
        reasons = {m["criterion"]: m["reason"] for m in rep["mismatches"]}
        self.assertIn("в другой реплике: [3]", reasons["needs"])
        self.assertIn("многоточием", reasons["consultation"])


@unittest.skipUnless(importlib.util.find_spec("fastapi") and sys.version_info >= (3, 10),
                     "server needs Python 3.10+ with server/requirements.txt (.venv)")
class ServerCacheTest(unittest.TestCase):
    """The export uses exactly the server's cache key and the model's original answers by default."""

    def test_same_cache_key_and_original_answers(self):
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import evaluator
        import server.app as app
        import server.evaluations as evaluations
        record = {"conversation_id": "test-cache-1", "channel": "agent chat", "language": "RU",
                  "transcript": [{"timestamp": "00:01", "speaker": "client", "text": "Где тариф?"},
                                 {"timestamp": "00:20", "speaker": "operator", "text": "Здравствуйте! В профиле."},
                                 {"timestamp": "00:30", "speaker": "operator", "text": "Всего доброго!"}]}

        seen_checklists = []

        def fake_engine(rec, checklist):
            seen_checklists.append(checklist)
            details = {c["id"]: {"explanation": "тест", "evidence": [{"message_index": 1, "quote": "В профиле."}]}
                       for c in checklist["criteria"]}
            answers = {**ANSWERS, "closing": "yes"}
            result = evaluator.build_result({**rec, "conversation_id": rec["id"]}, checklist, answers, details,
                                            "fake-model", 0.01)
            return result, {"seconds": 0.1, "input_tokens": 1, "output_tokens": 1}

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(app, "DB_PATH", Path(tmp) / "test.sqlite3"), \
                mock.patch.object(evaluations, "_run_engine", side_effect=fake_engine) as engine:
            app._init_db()
            app.create_conversation(record)
            std_id, std_file = ex.standard_checklists()["agent chat"]
            self.assertIsNone(ex._evaluate(evaluations, "test-cache-1", std_id))
            self.assertEqual(engine.call_count, 1)
            db_rec = app.get_conversation("test-cache-1")
            row = ex._standard_checklist(app, "agent chat")
            self.assertEqual(row["id"], std_id)
            key = ex.server_cache_key(db_rec, row)
            cached = ex._cached_row(app, key)
            self.assertIsNotNone(cached, "export key must match the key the server stored")
            self.assertEqual(ex.checklist_warnings(app), [])

            # the site activates an edited checklist: export still uses the standard one and only warns
            edited = {**json.loads(row["checklist_json"]), "id": std_id, "notes": "изменено на сайте"}
            new = app.create_checklist(edited)
            app.activate_checklist(new["id"])
            warnings = ex.checklist_warnings(app)
            self.assertEqual(len(warnings), 1)
            self.assertIn(new["id"], warnings[0])
            self.assertEqual(ex._standard_checklist(app, "agent chat")["id"], std_id)
            self.assertEqual(ex._cached_row(app, key)["id"], cached["id"])  # same cache, not paid again
            app.create_conversation({**record, "conversation_id": "test-cache-2"})
            self.assertIsNone(ex._evaluate(evaluations, "test-cache-2", std_id))
            self.assertEqual(seen_checklists[-1], std_file)  # evaluated by the standard checklist, not the edited one
            self.assertEqual(engine.call_count, 2)
            # a reviewer changes closing yes -> no; export keeps the model's answer unless --include-decisions
            ev_id = cached["id"]
            appeal = evaluations.create_appeal(ev_id, {"criterion_id": "closing", "comment": "нет итога"})
            evaluations.decide_appeal(appeal["id"], {"decision": "change", "new_answer": "no", "reason": "проверено"})
            cached = ex._cached_row(app, key)  # still found: appealed evaluations are not paid twice
            _, original, _ = ex._from_row(app, cached, include_decisions=False)
            _, decided, _ = ex._from_row(app, cached, include_decisions=True)
            self.assertEqual(original["closing"], "yes")
            self.assertEqual(decided["closing"], "no")


class ExpectOkTest(unittest.TestCase):
    def run_main(self, results, *flags):
        info = {"stopped": False, "results": results, "models": ["m"], "items": [], "missing": [],
                "other": {}, "new_cost": 0.0, "by_model": __import__("collections").Counter({"m": 1})}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ex, "load_source", return_value=([{"conversation_id": "c1"}], Path(tmp) / "d.json")), \
                mock.patch.object(ex, "_server", return_value=(None, None)), \
                mock.patch.object(ex, "collect", return_value=info), \
                mock.patch.object(sys, "argv", ["x", "--set", "prod", "--out", str(Path(tmp) / "r.json"), *flags]), \
                mock.patch("builtins.print"):
            with self.assertRaises(SystemExit) as ctx:
                ex.main()
        return ctx.exception.code

    def test_expect_ok(self):
        ok = ex.ok_record("c1", CHAT, ANSWERS, DETAILS)
        bad = ex.error_record("c2", "сбой")
        self.assertEqual(ex.error_ids([ok, bad]), ["c2"])
        self.assertEqual(self.run_main([ok, bad], "--expect-ok"), 3)
        self.assertEqual(self.run_main([ok, bad]), 0)          # without the flag errors do not fail the run
        self.assertEqual(self.run_main([ok], "--expect-ok"), 0)


class PickRowTest(unittest.TestCase):
    def test_pick_row(self):
        rows = [{"model": "claude-opus-5-5", "id": 2}, {"model": "claude-sonnet-5-5", "id": 1}]  # newest first
        self.assertEqual(ex.pick_row(rows, None), (rows[0], None))
        self.assertEqual(ex.pick_row(rows, "claude-sonnet-5-5"), (rows[1], None))
        self.assertEqual(ex.pick_row(rows, "gemini-x"), (None, rows[0]))
        self.assertEqual(ex.pick_row([], "gemini-x"), (None, None))


@unittest.skipUnless(importlib.util.find_spec("fastapi") and sys.version_info >= (3, 10),
                     "server needs Python 3.10+ with server/requirements.txt (.venv)")
class RequireModelTest(unittest.TestCase):
    """A cached evaluation by another model is re-evaluated by the required one; no network (llm.call is faked)."""

    def test_other_model_is_reevaluated_by_required(self):
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import evaluator
        import llm
        import server.app as app
        import server.evaluations as evaluations
        record = {"conversation_id": "test-model-1", "channel": "agent chat", "language": "RU",
                  "transcript": [{"timestamp": "00:01", "speaker": "client", "text": "Где тариф?"},
                                 {"timestamp": "00:20", "speaker": "operator", "text": "Здравствуйте! В профиле."},
                                 {"timestamp": "00:30", "speaker": "operator", "text": "Всего доброго!"}]}
        answers = {"contact": "yes", "needs": "na", "identification": "na", "consultation": "15", "empathy": "yes",
                   "dialog_management": "7", "closing": "no", "gross_errors": "no"}
        reply = json.dumps({cid: {"explanation": "тест", "evidence": {"message_index": 1, "quote": "В профиле."},
                                  "answer": a} for cid, a in answers.items()}, ensure_ascii=False)

        def opus_engine(rec, checklist):  # an older evaluation made by a fallback model
            details = {c["id"]: {"explanation": "старое", "evidence": []} for c in checklist["criteria"]}
            res = evaluator.build_result({**rec, "conversation_id": rec["id"]}, checklist,
                                         {**answers, "consultation": 20.0}, details, "claude-opus-5-5", 0.03)
            return res, {"seconds": 0.1, "input_tokens": 1, "output_tokens": 1}

        quiet = lambda *a, **k: None  # noqa: E731
        env_before = os.environ.get("EVAL_MODELS")
        original_evaluate = evaluator.evaluate
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(app, "DB_PATH", Path(tmp) / "t.sqlite3"), \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key",
                                             "EVAL_MODELS": "claude-sonnet-5-5,claude-opus-5-5"}):
            app._init_db()
            app.create_conversation(record)
            std_id = ex.standard_checklists()["agent chat"][0]
            with mock.patch.object(evaluations, "_run_engine", side_effect=opus_engine):
                self.assertIsNone(ex._evaluate(evaluations, "test-model-1", std_id))
            fake = llm.LLMResult(reply, 1000, 200, 0.5, 0.004, {"effort": "low"})
            with mock.patch("llm.call", return_value=fake) as call:
                info = ex.collect(app, evaluations, [record], required="claude-sonnet-5-5", log=quiet)
                self.assertEqual(info["other"], {"test-model-1": "claude-opus-5-5"})
                self.assertEqual(info["missing"], ["test-model-1"])
                self.assertEqual(call.call_count, 1)
                self.assertEqual(call.call_args[0][0].name, "claude-sonnet-5-5")  # forced model, no fallback chain
                self.assertEqual(dict(info["by_model"]), {"claude-sonnet-5-5": 1})
                self.assertEqual(info["results"][0]["criteria"][3]["value"], 15)  # new answer, not opus's 20
                self.assertEqual(os.environ["EVAL_MODELS"], "claude-sonnet-5-5,claude-opus-5-5")  # restored
                self.assertIs(evaluator.evaluate, original_evaluate)  # restored after the export

                again = ex.collect(app, evaluations, [record], required="claude-sonnet-5-5", log=quiet)
                self.assertEqual((again["missing"], call.call_count), ([], 1))  # now cached, not paid again
                anyone = ex.collect(app, evaluations, [record], required=None, log=quiet)
                self.assertEqual(anyone["missing"], [])
                stopped = ex.collect(app, evaluations, [record], required="gemini-3.5-flash-lite",
                                     only_cached=True, log=quiet)
                self.assertTrue(stopped["stopped"])
                self.assertEqual(call.call_count, 1)
        self.assertEqual(os.environ.get("EVAL_MODELS"), env_before)


if __name__ == "__main__":
    unittest.main()
