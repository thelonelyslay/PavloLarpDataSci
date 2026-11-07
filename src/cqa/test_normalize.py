"""Normalization of incoming dialog records (no network). Run: python3 -m unittest -v test_normalize"""
import json
import unittest
from pathlib import Path

from normalize import normalize_record

ROOT = Path(__file__).resolve().parents[2]


def rec(language="RU", channel="agent chat", speakers=("client", "operator")):
    return {"conversation_id": "c1", "language": language, "channel": channel,
            "transcript": [{"timestamp": "00:0%d" % i, "speaker": s, "text": f"t{i}"} for i, s in enumerate(speakers)]}


class NormalizeTest(unittest.TestCase):
    def test_language(self):
        for raw, want in (("ru", "RU"), ("kk", "KZ"), ("KZ", "KZ"), ("kz", "KZ"), ("en", "EN"), (" En ", "EN")):
            self.assertEqual(normalize_record(rec(language=raw))[0]["language"], want, raw)
        self.assertEqual(normalize_record(rec(language="uz"))[0]["language"], "UZ")  # unknown: kept (upper)

    def test_channel(self):
        for raw in ("chat", "agent_chat", "Agent Chat", "agent chat"):
            self.assertEqual(normalize_record(rec(channel=raw))[0]["channel"], "agent chat", raw)
        for raw in ("call", "voice", "voice_call", "Voice Call"):
            self.assertEqual(normalize_record(rec(channel=raw))[0]["channel"], "voice call", raw)
        self.assertEqual(normalize_record(rec(channel="email"))[0]["channel"], "email")  # unknown: kept

    def test_speakers_order_and_count_kept(self):
        out, warnings = normalize_record(rec(speakers=("customer", "Agent", "bot", "manager", "system", "robot")))
        self.assertEqual([m["speaker"] for m in out["transcript"]],
                         ["client", "operator", "bot", "operator", "system", "robot"])
        self.assertEqual([m["text"] for m in out["transcript"]], [f"t{i}" for i in range(6)])
        self.assertEqual(warnings, [])

    def test_no_operator_warns_instead_of_failing(self):
        out, warnings = normalize_record(rec(speakers=("client", "bot", "robot")))
        self.assertEqual(len(warnings), 1)
        self.assertIn("нет ни одной реплики оператора", warnings[0])
        self.assertEqual(len(out["transcript"]), 3)

    def test_input_is_not_modified(self):
        original = rec(language="ru", speakers=("agent",))
        snapshot = json.dumps(original, sort_keys=True)
        normalize_record(original)
        self.assertEqual(json.dumps(original, sort_keys=True), snapshot)

    @unittest.skipUnless((ROOT / "data" / "raw" / "dev.json").exists(), "no local dev data")
    def test_dev_data_is_already_canonical(self):
        # canonical data must not change: cache keys and stored evaluations stay valid
        records = json.loads((ROOT / "data" / "raw" / "dev.json").read_text(encoding="utf-8"))["data"]["records"]
        for r in records:
            out, _ = normalize_record(r)
            self.assertEqual(out, r, r["conversation_id"])


if __name__ == "__main__":
    unittest.main()
