"""evaluator.evaluate with a fake LLM (no network, no data/ files). Run: python3 -m unittest -v test_evaluator"""
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


def model_reply(answers=ANSWERS) -> str:
    return json.dumps({cid: {"quote": "Всего доброго!" if cid == "closing" else "",
                             "message_index": 3 if cid == "closing" else -1,
                             "explanation": f"тест {cid}", "answer": a} for cid, a in answers.items()},
                      ensure_ascii=False)


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


if __name__ == "__main__":
    unittest.main()
