"""evaluator.evaluate with a fake LLM (no network, no data/ files). Run: python3 -m unittest -v test_evaluator"""
import copy
import json
import unittest
from pathlib import Path
from unittest import mock

import evaluator
import llm

CHECKLIST = evaluator.load_checklist(Path(__file__).parent / "checklist.default.json")
ENV = {"ANTHROPIC_API_KEY": "test", "EVAL_MODELS": "claude-sonnet-5-5,claude-haiku-5-5"}
RECORD = {
    "conversation_id": "test-001", "channel": "agent chat", "language": "RU",
    "transcript": [
        {"timestamp": "00:01", "speaker": "client", "text": "Где посмотреть мой тариф?"},
        {"timestamp": "00:20", "speaker": "operator", "text": "Здравствуйте! Меня зовут Анна. Тариф виден в профиле."},
        {"timestamp": "00:40", "speaker": "client", "text": "Спасибо"},
        {"timestamp": "00:45", "speaker": "operator", "text": "Всего доброго!"},
    ],
}
# contact yes(10), needs na, identification na, consultation 15/20 of 20, empathy yes(15),
# dialog_management 7/10 of 15, closing no(5), gross no.
# applicable weight 65, earned 10+15+15+10.5+0 = 50.5 -> 77.69
ANSWERS = {"contact": "yes", "needs": "na", "identification": "na", "consultation": "15", "empathy": "yes",
           "dialog_management": "7", "closing": "no", "gross_errors": "no"}
TOP_KEYS = {"id", "conversation_id", "checklist_id", "checklist_version", "model", "cost_usd", "status",
            "total", "original_total", "section_scores", "gross_error", "criteria"}
CRIT_KEYS = {"id", "title", "answer", "original_answer", "final_answer", "applicable", "weight",
             "effective_weight", "points", "explanation", "na_reason", "evidence", "checked_range", "appeal"}


def model_reply(answers=ANSWERS, flat=False) -> str:
    out = {}
    for cid, a in answers.items():
        ev = {"message_index": 3 if cid == "closing" else -1, "quote": "Всего доброго!" if cid == "closing" else ""}
        out[cid] = {"explanation": f"тест {cid}", **(ev if flat else {"evidence": ev}), "answer": a}
    return json.dumps(out, ensure_ascii=False)


def fake_result(text: str) -> llm.LLMResult:
    return llm.LLMResult(text, 1000, 200, 0.5, 0.004, {"effort": "low"})


class EvaluateTest(unittest.TestCase):
    def test_contract_fields_and_total(self):
        with mock.patch("llm.call", return_value=fake_result(model_reply())) as call:
            stats = {}
            res = evaluator.evaluate(RECORD, CHECKLIST, env=ENV, stats=stats)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(set(res), TOP_KEYS)
        self.assertEqual(res["model"], "claude-sonnet-5-5")  # first in EVAL_MODELS
        self.assertEqual(res["status"], "done")
        self.assertEqual(res["total"], 77.69)
        self.assertEqual(res["original_total"], 77.69)
        self.assertFalse(res["gross_error"])
        self.assertEqual(res["checklist_id"], "checklist.default")
        self.assertEqual(res["checklist_version"], CHECKLIST["version"])
        self.assertAlmostEqual(res["cost_usd"], 0.004)
        self.assertEqual([c["id"] for c in res["criteria"]], [c for c in ANSWERS])
        for c in res["criteria"]:
            self.assertEqual(set(c), CRIT_KEYS)
            self.assertEqual(c["answer"], c["final_answer"])
            self.assertIsNone(c["appeal"])
            self.assertEqual(c["checked_range"], [0, 3])
        by = {c["id"]: c for c in res["criteria"]}
        self.assertEqual(by["consultation"]["answer"], 15)
        self.assertEqual(by["needs"]["na_reason"], "тест needs")
        self.assertFalse(by["needs"]["applicable"])
        self.assertEqual(by["closing"]["evidence"], [{"message_index": 3, "quote": "Всего доброго!"}])
        self.assertEqual(by["closing"]["points"], 0.0)
        self.assertEqual(stats["model"], "claude-sonnet-5-5")

    def test_fallback_to_next_model(self):
        replies = [llm.LLMError("HTTP 529: overloaded", 529), llm.LLMError("HTTP 529: overloaded", 529),
                   fake_result(model_reply())]
        with mock.patch("llm.call", side_effect=replies) as call:
            res = evaluator.evaluate(RECORD, CHECKLIST, env=ENV)
        self.assertEqual(call.call_count, 3)
        self.assertEqual(call.call_args[0][0].name, "claude-haiku-5-5")
        self.assertEqual(res["model"], "claude-haiku-5-5")
        self.assertEqual(res["total"], 77.69)

    def test_invalid_answer_is_retried(self):
        bad = model_reply({**ANSWERS, "closing": "maybe"})
        with mock.patch("llm.call", side_effect=[fake_result(bad), fake_result(model_reply())]):
            res = evaluator.evaluate(RECORD, CHECKLIST, env=ENV)
        self.assertEqual(res["model"], "claude-sonnet-5-5")

    def test_explicit_model_has_no_fallback(self):
        with mock.patch("llm.call", side_effect=llm.LLMError("HTTP 500", 500)) as call:
            with self.assertRaises(evaluator.EvaluationError):
                evaluator.evaluate(RECORD, CHECKLIST, "claude-opus-5-5", env=ENV)
        self.assertEqual(call.call_count, evaluator.ATTEMPTS_PER_MODEL)

    def test_all_models_fail(self):
        with mock.patch("llm.call", side_effect=llm.LLMError("HTTP 500", 500)):
            with self.assertRaises(evaluator.EvaluationError) as ctx:
                evaluator.evaluate(RECORD, CHECKLIST, env=ENV)
        self.assertIn("claude-haiku-5-5", str(ctx.exception))

    def test_4xx_is_not_retried(self):
        replies = [llm.LLMError("HTTP 401: bad key", 401), fake_result(model_reply())]
        with mock.patch("llm.call", side_effect=replies) as call:
            res = evaluator.evaluate(RECORD, CHECKLIST, env=ENV)
        self.assertEqual(call.call_count, 2)  # sonnet once (no retry), then haiku
        self.assertEqual(res["model"], "claude-haiku-5-5")

    def test_time_limit(self):
        with mock.patch("llm.call") as call:
            with self.assertRaises(evaluator.EvaluationError) as ctx:
                evaluator.evaluate(RECORD, CHECKLIST, env={**ENV, "EVAL_TIMEOUT": "0"})
        call.assert_not_called()
        self.assertIn("EVAL_TIMEOUT", str(ctx.exception))
        with mock.patch("llm.call", side_effect=llm.DeadlineError("превышен общий предел времени")) as call:
            with self.assertRaises(evaluator.EvaluationError):
                evaluator.evaluate(RECORD, CHECKLIST, env=ENV)
        self.assertEqual(call.call_count, 1)  # no retries and no next model after the deadline

    def test_checklist_by_channel(self):
        call_record = {**RECORD, "conversation_id": "test-call", "channel": "voice call"}
        with mock.patch("llm.call", return_value=fake_result(model_reply())):
            self.assertEqual(evaluator.evaluate(call_record, env=ENV)["checklist_id"], "checklist.calls")
            self.assertEqual(evaluator.evaluate(RECORD, env=ENV)["checklist_id"], "checklist.default")

    def test_old_flat_reply_still_parses(self):
        with mock.patch("llm.call", return_value=fake_result(model_reply(flat=True))):
            res = evaluator.evaluate(RECORD, CHECKLIST, env=ENV)
        self.assertEqual({c["id"]: c for c in res["criteria"]}["closing"]["evidence"][0]["message_index"], 3)


class ChecklistValidationTest(unittest.TestCase):
    def check(self, mutate, *phrases):
        ck = copy.deepcopy(CHECKLIST)
        mutate(ck)
        with self.assertRaises(evaluator.ChecklistError) as ctx:
            evaluator.validate_checklist_fields(ck)
        for p in phrases:
            self.assertIn(p, str(ctx.exception))

    def test_flag_weight(self):
        def m(ck):
            next(c for c in ck["criteria"] if c["type"] == "flag")["weight"] = 10
        self.check(m, "у флага не может быть веса")

    def test_missing_title_and_weight(self):
        def m(ck):
            del ck["criteria"][0]["title"]
            del ck["criteria"][1]["weight"]
        self.check(m, "нет названия (title)", "вес (weight) не указан")

    def test_bad_scale_and_type(self):
        def m(ck):
            next(c for c in ck["criteria"] if c["type"] == "scale")["steps"] = [10]
            ck["criteria"][0]["type"] = "stars"
        self.check(m, "steps", "неизвестный тип")

    def test_evaluate_rejects_bad_checklist(self):
        ck = copy.deepcopy(CHECKLIST)
        del ck["criteria"][0]["title"]
        with mock.patch("llm.call") as call:
            with self.assertRaises(evaluator.ChecklistError):
                evaluator.evaluate(RECORD, ck, env=ENV)
        call.assert_not_called()

    def test_real_checklists_are_valid(self):
        for name in ("checklist.default.json", "checklist.calls.json"):
            evaluator.load_checklist(Path(__file__).parent / name)


class LLMTest(unittest.TestCase):
    def test_prices_for_other_gemini_model(self):
        env = {"GEMINI_API_KEY": "x", "GEMINI_MODEL": "gemini-3.8-flash"}
        spec = llm.resolve_model("gemini-3.5-flash-lite", env)
        self.assertEqual((spec.price_in, spec.price_out), llm.DEFAULT_PRICES["gemini-3.5-flash-lite"])
        spec = llm.resolve_model("gemini-9-test", {**env, "MODEL_PRICES": "gemini-9-test:1.5/6"})
        self.assertEqual((spec.price_in, spec.price_out), (1.5, 6.0))
        self.assertIsNone(llm.resolve_model("gemini-unknown", env).price_in)

    def test_retry_after_is_capped(self):
        busy = mock.Mock(status_code=429, headers={"retry-after": "999"}, text="busy")
        ok = mock.Mock(status_code=200, headers={}, text="{}")
        ok.json.return_value = {"ok": True}
        with mock.patch("requests.post", side_effect=[busy, ok]), mock.patch("time.sleep") as sleep:
            self.assertEqual(llm._post("http://x", {}, {}, {}), {"ok": True})
        sleep.assert_called_once_with(llm.MAX_RETRY_AFTER)

    def test_4xx_fails_at_once(self):
        bad = mock.Mock(status_code=400, headers={}, text="bad request")
        with mock.patch("requests.post", return_value=bad) as post:
            with self.assertRaises(llm.LLMError) as ctx:
                llm._post("http://x", {}, {}, {})
        self.assertEqual(post.call_count, 1)
        self.assertFalse(ctx.exception.retryable)

    def test_gemini_schema_keeps_order(self):
        from prompting import build_schema
        s = llm._gemini_schema(build_schema(CHECKLIST))
        crit = s["properties"]["closing"]
        self.assertEqual(crit["propertyOrdering"], ["explanation", "evidence", "answer"])
        self.assertNotIn("additionalProperties", crit)


if __name__ == "__main__":
    unittest.main()
