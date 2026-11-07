"""Server integration point for the team's deterministic scoring module."""
from __future__ import annotations

from typing import Any

from src.cqa.scoring import score


def score_answers(checklist: dict, answers: dict[str, Any]) -> dict:
    """Score model or reviewer answers using the single shared implementation."""
    return score(checklist, answers)
