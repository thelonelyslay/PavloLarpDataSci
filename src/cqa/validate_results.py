"""Check a results file in the organizers' JSON 1.0 format. Messages are in Russian, exit code 1 on violations.

Usage:
  python3 src/cqa/validate_results.py results/dev/results.json --dialogs data/raw/dev.json --expect 65

--dialogs (same format as data/raw/dev.json) is needed to check evidence_turns against the real number
of messages and to recompute final_score with the checklist of the dialog's channel.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

import scoring
from normalize import normalize_record

HERE = Path(__file__).resolve().parent
SCHEMA_VERSION = "1.0"
MODES = ("local", "external_api", "hybrid")
# our criterion id -> organizers' criterion id, in the organizers' order
CRITERIA_MAP = [
    ("contact", "contact"),
    ("needs", "discovery"),
    ("identification", "identification"),
    ("consultation", "consultation"),
    ("empathy", "empathy"),
    ("dialog_management", "dialogue"),
    ("closing", "closure"),
    ("gross_errors", "critical"),
]
ORG_TO_OURS = {org: ours for ours, org in CRITERIA_MAP}
ORG_IDS = [org for _, org in CRITERIA_MAP]
# allowed values: "bool" (true/false), "bool_or_null", or a set of numbers (+ None if null allowed)
ALLOWED = {
    "contact": "bool", "discovery": "bool_or_null", "identification": "bool_or_null",
    "consultation": {0, 5, 10, 15, 20, None}, "empathy": "bool", "dialogue": {0, 4, 7, 10},
    "closure": "bool_or_null", "critical": "bool",
}
TOP_KEYS = {"schema_version", "team_name", "model", "results"}
OK_KEYS = {"conversation_id", "status", "criteria", "score"}
ERROR_KEYS = {"conversation_id", "status", "criteria", "score", "error"}
CRIT_KEYS = {"criterion_id", "value", "rationale", "evidence_turns"}
SCORE_KEYS = {"final_score", "critical_flag"}


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def to_ours(value: Any) -> Any:
    """Organizers' value -> our answer: true/false/null -> yes/no/na, numbers stay."""
    if value is True:
        return "yes"
    if value is False:
        return "no"
    if value is None:
        return "na"
    return value


def load_checklists() -> tuple[dict, dict]:
    by_channel, default = {}, None
    for name in ("checklist.default.json", "checklist.calls.json"):
        ck = json.loads((HERE / name).read_text(encoding="utf-8"))
        by_channel.setdefault(ck.get("applies_to"), ck)
        default = default or ck
    return by_channel, default


def load_dialogs(path: Path) -> dict[str, dict]:
    records = [normalize_record(r)[0] for r in json.loads(path.read_text(encoding="utf-8"))["data"]["records"]]
    return {str(r["conversation_id"]): r for r in records}


def parse_strict(raw: bytes) -> tuple[Optional[Any], list[str]]:
    """UTF-8 without BOM, a single JSON value, no duplicate keys, no NaN/Infinity."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return None, ["файл начинается с BOM: нужен UTF-8 без BOM"]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return None, [f"файл не в кодировке UTF-8 (байт {e.start})"]
    dups: list[str] = []

    def no_dups(pairs):
        keys = [k for k, _ in pairs]
        for k, n in Counter(keys).items():
            if n > 1:
                dups.append(f"ключ «{k}» повторяется в одном объекте")
        return dict(pairs)

    def bad_constant(name):
        raise ValueError(f"недопустимое значение {name}")

    try:
        data = json.loads(text, object_pairs_hook=no_dups, parse_constant=bad_constant)
    except (json.JSONDecodeError, ValueError) as e:
        return None, [f"это не корректный JSON: {e}"]
    return data, dups


def _extra(keys: set, allowed: set, where: str) -> list[str]:
    out = []
    if keys - allowed:
        out.append(f"{where}: лишние поля {sorted(keys - allowed)}")
    if allowed - keys:
        out.append(f"{where}: нет полей {sorted(allowed - keys)}")
    return out


def check_value(org_id: str, value: Any) -> Optional[str]:
    rule = ALLOWED[org_id]
    if rule == "bool":
        return None if isinstance(value, bool) else f"нужно true или false, сейчас {json.dumps(value, ensure_ascii=False)}"
    if rule == "bool_or_null":
        return None if value is None or isinstance(value, bool) else \
            f"нужно true, false или null, сейчас {json.dumps(value, ensure_ascii=False)}"
    if value is None:
        return None if None in rule else "null здесь недопустим"
    if not _is_number(value) or float(value) not in {float(x) for x in rule if x is not None}:
        allowed = ", ".join(str(x) for x in sorted(x for x in rule if x is not None)) + (", null" if None in rule else "")
        return f"допустимо только {allowed}, сейчас {json.dumps(value, ensure_ascii=False)}"
    return None


def validate(data: Any, dialogs: Optional[dict[str, dict]] = None, expect: Optional[int] = None) -> list[str]:
    """Return a list of violations in Russian (empty if the file is correct)."""
    errs: list[str] = []
    if not isinstance(data, dict):
        return ["верхний уровень файла должен быть объектом JSON"]
    errs += _extra(set(data), TOP_KEYS, "файл")
    if data.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"schema_version должен быть «{SCHEMA_VERSION}», сейчас {json.dumps(data.get('schema_version'), ensure_ascii=False)}")
    if not isinstance(data.get("team_name"), str) or not data["team_name"].strip():
        errs.append("team_name должен быть непустой строкой")
    model = data.get("model")
    if not isinstance(model, dict):
        errs.append("model должен быть объектом с полями mode и name")
    else:
        errs += _extra(set(model), {"mode", "name"}, "model")
        if model.get("mode") not in MODES:
            errs.append(f"model.mode должен быть одним из {', '.join(MODES)}, сейчас {json.dumps(model.get('mode'), ensure_ascii=False)}")
        if not isinstance(model.get("name"), str) or not model["name"].strip():
            errs.append("model.name должен быть непустой строкой")
    results = data.get("results")
    if not isinstance(results, list):
        return errs + ["results должен быть списком"]
    if expect is not None and len(results) != expect:
        errs.append(f"записей в results {len(results)}, ожидалось {expect}")

    by_channel, default_ck = load_checklists()
    ids = Counter(r.get("conversation_id") for r in results if isinstance(r, dict))
    for cid, n in ids.items():
        if n > 1:
            errs.append(f"диалог {cid}: встречается {n} раза, id должны быть уникальными")
    if dialogs is not None:
        for cid in ids:
            if cid not in dialogs:
                errs.append(f"диалог {cid}: нет в файле с диалогами")

    for i, r in enumerate(results, 1):
        if not isinstance(r, dict):
            errs.append(f"запись №{i}: должна быть объектом")
            continue
        cid = r.get("conversation_id")
        where = f"диалог {cid}" if isinstance(cid, str) and cid else f"запись №{i}"
        if not isinstance(cid, str) or not cid.strip():
            errs.append(f"{where}: нет conversation_id")
        status = r.get("status")
        if status == "error":
            errs += _extra(set(r), ERROR_KEYS, where)
            if r.get("criteria") != []:
                errs.append(f"{where}: у записи с ошибкой criteria должен быть пустым списком")
            if r.get("score") is not None:
                errs.append(f"{where}: у записи с ошибкой score должен быть null")
            if not isinstance(r.get("error"), str) or not r["error"].strip():
                errs.append(f"{where}: у записи с ошибкой должна быть причина в поле error")
            continue
        if status != "ok":
            errs.append(f"{where}: status должен быть «ok» или «error», сейчас {json.dumps(status, ensure_ascii=False)}")
            continue
        errs += _extra(set(r), OK_KEYS, where)
        dialog = (dialogs or {}).get(cid)
        n_turns = len(dialog["transcript"]) if dialog else None
        crits = r.get("criteria")
        if not isinstance(crits, list):
            errs.append(f"{where}: criteria должен быть списком")
            continue
        if len(crits) != len(ORG_IDS):
            errs.append(f"{where}: критериев {len(crits)}, нужно ровно {len(ORG_IDS)}")
        seen = Counter(c.get("criterion_id") for c in crits if isinstance(c, dict))
        for org_id in ORG_IDS:
            if seen[org_id] == 0:
                errs.append(f"{where}: нет критерия {org_id}")
            elif seen[org_id] > 1:
                errs.append(f"{where}: критерий {org_id} встречается {seen[org_id]} раза")
        for unknown in set(seen) - set(ORG_IDS):
            errs.append(f"{where}: неизвестный критерий {json.dumps(unknown, ensure_ascii=False)}")
        values: dict[str, Any] = {}
        for c in crits:
            if not isinstance(c, dict):
                errs.append(f"{where}: каждый критерий должен быть объектом")
                continue
            org_id = c.get("criterion_id")
            cw = f"{where}, {org_id}"
            errs += _extra(set(c), CRIT_KEYS, cw)
            if org_id not in ALLOWED:
                continue
            problem = check_value(org_id, c.get("value"))
            if problem:
                errs.append(f"{cw}: {problem}")
            else:
                values[org_id] = c.get("value")
            if not isinstance(c.get("rationale"), str) or not c["rationale"].strip():
                errs.append(f"{cw}: rationale должен быть непустой строкой")
            turns = c.get("evidence_turns")
            if not isinstance(turns, list):
                errs.append(f"{cw}: evidence_turns должен быть списком")
                continue
            for t in turns:
                if not _is_int(t):
                    errs.append(f"{cw}: номер реплики {json.dumps(t, ensure_ascii=False)} должен быть целым числом")
                elif t < 1:
                    errs.append(f"{cw}: номер реплики {t} меньше 1 (нумерация с 1)")
                elif n_turns is not None and t > n_turns:
                    errs.append(f"{cw}: номер реплики {t} больше числа реплик в диалоге ({n_turns})")
            if len([t for t in turns if _is_int(t)]) != len({t for t in turns if _is_int(t)}):
                errs.append(f"{cw}: номера реплик повторяются")
        sc = r.get("score")
        if not isinstance(sc, dict):
            errs.append(f"{where}: score должен быть объектом с final_score и critical_flag")
            continue
        errs += _extra(set(sc), SCORE_KEYS, f"{where}, score")
        if not isinstance(sc.get("critical_flag"), bool):
            errs.append(f"{where}: critical_flag должен быть true или false")
        elif "critical" in values and sc["critical_flag"] != values["critical"]:
            errs.append(f"{where}: critical_flag ({json.dumps(sc['critical_flag'])}) не равен значению critical "
                        f"({json.dumps(values['critical'])})")
        final = sc.get("final_score")
        if final is not None and not _is_number(final):
            errs.append(f"{where}: final_score должен быть числом")
            continue
        if len(values) == len(ORG_IDS):
            ck = by_channel.get(dialog.get("channel"), default_ck) if dialog else default_ck
            try:
                expected = scoring.score(ck, {ORG_TO_OURS[k]: to_ours(v) for k, v in values.items()})["total"]
            except ValueError as e:
                errs.append(f"{where}: итог нельзя пересчитать по чек-листу: {e}")
                continue
            if (expected is None) != (final is None) or (expected is not None and abs(expected - final) > 1e-9):
                errs.append(f"{where}: final_score {final} не совпадает с пересчётом по критериям ({expected})")
    return errs


def summary(data: dict, dialogs: Optional[dict[str, dict]]) -> str:
    results = data.get("results") or []
    st = Counter(r.get("status") for r in results if isinstance(r, dict))
    line = f"записей: {len(results)} (ok: {st.get('ok', 0)}, error: {st.get('error', 0)})"
    if dialogs:
        ch = Counter(dialogs.get(r.get("conversation_id"), {}).get("channel", "?") for r in results if isinstance(r, dict))
        line += "; по каналам: " + ", ".join(f"{k}: {v}" for k, v in sorted(ch.items()))
    return line


def validate_file(path: Path, dialogs_path: Optional[Path] = None, expect: Optional[int] = None) -> int:
    data, errs = parse_strict(Path(path).read_bytes())
    dialogs = load_dialogs(dialogs_path) if dialogs_path else None
    if data is not None:
        errs += validate(data, dialogs, expect)
    if dialogs is None:
        print("предупреждение: без --dialogs не проверены верхняя граница evidence_turns и канал диалога")
    if errs:
        print(f"Файл {path}: нарушений {len(errs)}")
        for e in errs:
            print(" -", e)
        return 1
    print(f"Файл {path}: нарушений нет. {summary(data, dialogs)}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="results JSON file")
    ap.add_argument("--dialogs", help="dialogs file (data/raw/dev.json format)")
    ap.add_argument("--expect", type=int, help="expected number of records (dev 65, prod 10)")
    args = ap.parse_args()
    sys.exit(validate_file(Path(args.path), Path(args.dialogs) if args.dialogs else None, args.expect))


if __name__ == "__main__":
    main()
