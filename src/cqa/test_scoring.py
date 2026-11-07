import copy
import json
import unittest
from pathlib import Path

from scoring import score

CFG = json.loads((Path(__file__).parent / "checklist.default.json").read_text(encoding="utf-8"))


def answers(**over):
    base = {"contact": "yes", "needs": "yes", "identification": "yes", "consultation": 20,
            "empathy": "yes", "dialog_management": 10, "closing": "yes", "gross_errors": "no"}
    base.update(over)
    return base


class ScoringTests(unittest.TestCase):
    def test_all_top(self):
        r = score(CFG, answers())
        self.assertEqual(r["total"], 100.0)
        self.assertFalse(r["gross_error"])

    def test_all_zero(self):
        r = score(CFG, answers(contact="no", needs="no", identification="no", consultation=0,
                               empathy="no", dialog_management=0, closing="no"))
        self.assertEqual(r["total"], 0.0)

    def test_scale_proportional(self):
        # 65 (yes/no) + 15 (consultation) + 7/10*15 = 10.5 -> 90.5
        r = score(CFG, answers(consultation=15, dialog_management=7))
        self.assertEqual(r["total"], 90.5)

    def test_na_redistribution(self):
        # needs (15) and identification (20) are N/A -> applicable 65, earned 55.5 -> 85.38
        r = score(CFG, answers(needs="na", identification="na", consultation=15, dialog_management=7))
        self.assertEqual(r["total"], 85.38)
        eff = {c["id"]: c["effective_weight"] for c in r["criteria"]}
        self.assertEqual(eff["needs"], 0.0)
        self.assertAlmostEqual(sum(eff.values()), 100.0, places=1)

    def test_points_sum_to_total(self):
        r = score(CFG, answers(needs="na", consultation=10, dialog_management=4, closing="no"))
        self.assertAlmostEqual(sum(c["points"] for c in r["criteria"]), r["total"], delta=0.05)

    def test_gross_error_flag_separate(self):
        r = score(CFG, answers(gross_errors="yes"))
        self.assertEqual(r["total"], 100.0)  # flag does not change points
        self.assertTrue(r["gross_error"])
        self.assertEqual(r["gross_error_ids"], ["gross_errors"])

    def test_na_not_allowed(self):
        with self.assertRaises(ValueError):
            score(CFG, answers(contact="na"))
        with self.assertRaises(ValueError):
            score(CFG, answers(dialog_management="na"))

    def test_invalid_scale_step(self):
        with self.assertRaises(ValueError):
            score(CFG, answers(consultation=12))

    def test_missing_answer(self):
        a = answers()
        del a["closing"]
        with self.assertRaises(ValueError):
            score(CFG, a)

    def test_russian_answers(self):
        r = score(CFG, answers(contact="Да", closing="Нет", needs="N/A"))
        self.assertEqual(r["total"], round((10 + 20 + 20 + 15 + 10 * 1.5) / 85 * 100, 2))

    def test_round_half_up(self):
        cfg = copy.deepcopy(CFG)
        cfg["criteria"] = [c for c in cfg["criteria"] if c["id"] in ("contact", "needs", "gross_errors")]
        cfg["criteria"][0]["weight"] = 1
        cfg["criteria"][1]["weight"] = 7  # 1/8 = 12.5 -> 12.5; use 1/16 below
        cfg["criteria"][1]["weight"] = 15  # 1/16 = 6.25 -> 6.25
        r = score(cfg, {"contact": "yes", "needs": "no", "gross_errors": "no"})
        self.assertEqual(r["total"], 6.25)

    def test_no_applicable_weight(self):
        cfg = copy.deepcopy(CFG)
        cfg["criteria"] = [dict(c, allow_na=True) for c in cfg["criteria"]]
        r = score(cfg, {c["id"]: "na" for c in cfg["criteria"] if c["type"] != "flag"} | {"gross_errors": "no"})
        self.assertIsNone(r["total"])

    def test_replaced_criterion(self):
        # control step: one criterion replaced via config, math still works
        cfg = copy.deepcopy(CFG)
        for c in cfg["criteria"]:
            if c["id"] == "closing":
                c.update(id="speed", title="Скорость", type="scale", weight=5, steps=[10, 5, 0], allow_na=False)
        a = answers()
        del a["closing"]
        a["speed"] = 5
        r = score(cfg, a)
        self.assertEqual(r["total"], 97.5)

    def test_section_scores(self):
        r = score(CFG, answers(needs="no"))
        self.assertEqual(r["section_scores"]["Начало и понимание запроса"], round(10 / 25 * 100, 2))


if __name__ == "__main__":
    unittest.main()
