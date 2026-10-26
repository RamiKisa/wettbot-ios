"""Small, deterministic parsers for public football-feed formats."""
from __future__ import annotations

from typing import Any


def match_rows(payload: Any) -> list[dict]:
    """Accept either OpenFootball's {matches: [...]} object or a top-level list."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("matches", [])
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def full_time_score(score: Any) -> tuple[Any, Any]:
    """Return the final home/away score for object and pair-list score encodings."""
    if isinstance(score, list):
        if len(score) >= 2:
            return score[0], score[1]
        return None, None
    if isinstance(score, dict):
        ft = score.get("ft") or score.get("fulltime")
        if isinstance(ft, dict):
            return ft.get("team1", ft.get("home")), ft.get("team2", ft.get("away"))
        if isinstance(ft, list) and len(ft) >= 2:
            return ft[0], ft[1]
        if "home" in score or "team1" in score:
            return score.get("team1", score.get("home")), score.get("team2", score.get("away"))
    return None, None
