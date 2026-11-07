"""CQA scoring: deterministic math for the checklist. The LLM only gives answers;
this module computes points, N/A weight redistribution, section scores and the total.

Answers format: {criterion_id: "yes" | "no" | "na" | <number step>}
For `flag` criteria (gross errors): "yes" = violation found, "no" = none. They carry no points.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

YES, NO, NA = "yes", "no", "na"


def _round(value: float, digits: int) -> float:
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def validate_checklist(checklist: dict) -> None:
    """Raise ValueError if the checklist config is inconsistent."""
    seen = set()
    for c in checklist["criteria"]:
        cid = c["id"]
        if cid in seen:
            raise ValueError(f"duplicate criterion id: {cid}")
        seen.add(cid)
        if c["type"] not in ("yes_no", "scale", "flag"):
            raise ValueError(f"{cid}: unknown type {c['type']}")
        if c["weight"] < 0:
            raise ValueError(f"{cid}: negative weight")
        if c["type"] == "scale":
            steps = c.get("steps") or []
            if len(steps) < 2 or any(s < 0 for s in steps):
                raise ValueError(f"{cid}: scale needs >= 2 non-negative steps")
            if max(steps) <= 0:
                raise ValueError(f"{cid}: top step must be > 0")


def _normalize(c: dict, raw: Any) -> Any:
    """Return 'yes'/'no'/'na' or a number step; raise ValueError if not allowed."""
    if isinstance(raw, str):
        v = raw.strip().lower()
        v = {"да": YES, "нет": NO, "n/a": NA, "н/п": NA, "true": YES, "false": NO}.get(v, v)
    else:
        v = raw
    if v == NA:
        if not c.get("allow_na"):
            raise ValueError(f"{c['id']}: N/A is not allowed")
        return NA
    if c["type"] in ("yes_no", "flag"):
        if v not in (YES, NO):
            raise ValueError(f"{c['id']}: expected yes/no, got {raw!r}")
        return v
    try:
        num = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{c['id']}: expected a number step, got {raw!r}") from None
    if num not in [float(s) for s in c["steps"]]:
        raise ValueError(f"{c['id']}: {num} is not one of steps {c['steps']}")
    return num


def score(checklist: dict, answers: dict[str, Any]) -> dict:
    """Compute the full result. Raises ValueError on invalid/missing answers."""
    validate_checklist(checklist)
    digits = checklist.get("rounding", 2)
    scored = [c for c in checklist["criteria"] if c["type"] != "flag"]
    flags = [c for c in checklist["criteria"] if c["type"] == "flag"]

    norm: dict[str, Any] = {}
    for c in checklist["criteria"]:
        if c["id"] not in answers:
            raise ValueError(f"missing answer for {c['id']}")
        norm[c["id"]] = _normalize(c, answers[c["id"]])

    # points before redistribution (in units of the criterion's own weight)
    earned: dict[str, float] = {}
    applicable: dict[str, bool] = {}
    for c in scored:
        a = norm[c["id"]]
        if a == NA:
            applicable[c["id"]] = False
            earned[c["id"]] = 0.0
            continue
        applicable[c["id"]] = True
        if c["type"] == "yes_no":
            earned[c["id"]] = float(c["weight"]) if a == YES else 0.0
        else:  # scale: top step gives the full weight
            earned[c["id"]] = a / float(max(c["steps"])) * c["weight"]

    applicable_weight = sum(c["weight"] for c in scored if applicable[c["id"]])
    total: float | None
    if applicable_weight <= 0:
        total = None  # no applicable criteria with weight -> total is not computed
    else:
        total = _round(sum(earned.values()) / applicable_weight * 100, digits)

    criteria_out = []
    for c in scored:
        cid = c["id"]
        if applicable[cid] and applicable_weight > 0:
            eff_weight = c["weight"] / applicable_weight * 100
            points = earned[cid] / applicable_weight * 100
        else:
            eff_weight, points = 0.0, 0.0
        criteria_out.append({
            "id": cid, "title": c["title"], "section": c.get("section"),
            "answer": norm[cid], "applicable": applicable[cid],
            "weight": c["weight"],
            "effective_weight": _round(eff_weight, digits),
            "points": _round(points, digits),
        })

    sections: dict[str, dict] = {}
    for c in scored:
        sec = c.get("section") or "—"
        s = sections.setdefault(sec, {"earned": 0.0, "weight": 0.0})
        if applicable[c["id"]]:
            s["earned"] += earned[c["id"]]
            s["weight"] += c["weight"]
    section_scores = {
        sec: (_round(v["earned"] / v["weight"] * 100, digits) if v["weight"] > 0 else None)
        for sec, v in sections.items()
    }

    gross = [c["id"] for c in flags if norm[c["id"]] == YES]
    return {
        "total": total,
        "criteria": criteria_out,
        "section_scores": section_scores,
        "gross_error": bool(gross),
        "gross_error_ids": gross,
    }
