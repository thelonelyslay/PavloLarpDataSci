"""labels_from_csv.build on temporary CSV files (no network, no data/). Run: python3 -m unittest -v test_labels_from_csv"""
import json
import tempfile
import unittest
from pathlib import Path

import labels_from_csv

HEADER = "conversation_id,criterion,answer,message_index,reason\n"


class LabelsFromCsvTest(unittest.TestCase):
    def build(self, files: dict, dev_records=None):
        with tempfile.TemporaryDirectory() as tmp:
            docs = Path(tmp) / "docs"
            docs.mkdir()
            for name, body in files.items():
                (docs / name).write_text(HEADER + body, encoding="utf-8")
            dev = Path(tmp) / "dev.json"
            if dev_records is not None:
                dev.write_text(json.dumps({"data": {"records": dev_records}}), encoding="utf-8")
            return labels_from_csv.build(docs, dev)

    def test_criterion_by_id_any_case_or_number(self):
        labels, conflicts, errors, _ = self.build({"labels_a.csv": (
            "c1,Contact,yes,1,id with capital\n"
            "c1,2,no,3,number 2 = needs\n"
            "c1,7,No,9,number 7 = closing\n"
            "c1,CONSULTATION,15,5,scale by id\n"
        )})
        self.assertEqual(errors, [])
        self.assertEqual(labels["c1"], {"contact": "yes", "needs": "no", "closing": "no", "consultation": 15})

    def test_na_spellings_and_case(self):
        labels, _, errors, _ = self.build({"labels_a.csv": (
            "c1,needs,n/a,,\nc2,needs,N/A,,\nc3,needs,NA,,\nc4,needs,na,,\nc5,contact,YES,,\nc6,contact,Нет,,\n"
        )})
        self.assertEqual(errors, [])
        self.assertEqual([labels[f"c{i}"]["needs"] for i in range(1, 5)], ["na"] * 4)
        self.assertEqual(labels["c5"]["contact"], "yes")
        self.assertEqual(labels["c6"]["contact"], "no")

    def test_empty_answers_skipped_and_bad_values_reported(self):
        labels, _, errors, _ = self.build({"labels_a.csv": (
            "c1,closing,,,\n"            # empty: skipped silently
            "c1,contact,na,,\n"          # N/A not allowed for contact
            "c1,99,yes,,\n"              # number out of range
            "c1,bonus,yes,,\n"           # unknown id
            "c1,consultation,12,,\n"     # not a scale step
        )})
        self.assertEqual(labels, {})
        self.assertEqual(len(errors), 4)
        self.assertTrue(any("out of range 1..8" in e for e in errors))
        self.assertTrue(any("unknown criterion 'bonus'" in e for e in errors))

    def test_conflicts_between_files_are_not_written(self):
        labels, conflicts, errors, n_files = self.build({
            "labels_a.csv": "c1,closing,no,11,Только попрощался, без итога\nc1,contact,yes,1,ok\n",
            "labels_b.csv": "c1,7,yes,11,Предложил помощь\nc1,Contact,YES,1,ok\n",
            "labels_template.csv": "c1,closing,na,,template is ignored\n",
        })
        self.assertEqual(n_files, 2)
        self.assertEqual(errors, [])
        self.assertEqual(labels, {"c1": {"contact": "yes"}})  # agreeing labels are kept
        votes = conflicts["c1"]["closing"]
        self.assertEqual(sorted(v["answer"] for v in votes), ["no", "yes"])
        self.assertIn("Только попрощался, без итога", [v["reason"] for v in votes])  # extra column glued back

    def test_call_dialog_uses_call_checklist(self):
        labels, _, errors, _ = self.build({"labels_a.csv": "call1,1,yes,,\n"},
                                          dev_records=[{"conversation_id": "call1", "channel": "voice call"}])
        self.assertEqual(errors, [])
        self.assertEqual(labels, {"call1": {"contact": "yes"}})


if __name__ == "__main__":
    unittest.main()
