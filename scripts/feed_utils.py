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

def _score_int(value: Any) -> int | None:
    """Parse ESPN score strings/numbers without accepting non-final placeholders."""
    if isinstance(value, dict):
        value = value.get("displayValue", value.get("value"))
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not number.is_integer() or number < 0:
        return None
    return int(number)


def espn_completed_match_rows(events: Any) -> list[dict]:
    """Normalize completed ESPN scoreboard events into one row per match."""
    if not isinstance(events, list):
        return []
    rows = []
    seen = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        competitions = event.get("competitions")
        if not isinstance(competitions, list) or not competitions or not isinstance(competitions[0], dict):
            continue
        competition = competitions[0]
        event_status = (event.get("status") or {}).get("type") or {}
        comp_status = (competition.get("status") or {}).get("type") or {}
        completed = (
            event_status.get("completed") is True
            or comp_status.get("completed") is True
            or event_status.get("state") == "post"
            or comp_status.get("state") == "post"
        )
        if not completed:
            continue
        competitors = competition.get("competitors")
        if not isinstance(competitors, list):
            continue
        home = next((x for x in competitors if isinstance(x, dict) and x.get("homeAway") == "home"), None)
        away = next((x for x in competitors if isinstance(x, dict) and x.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        home_team = home.get("team") or {}
        away_team = away.get("team") or {}
        home_name = home_team.get("displayName") or home_team.get("name")
        away_name = away_team.get("displayName") or away_team.get("name")
        match_date = event.get("date")
        home_goals = _score_int(home.get("score"))
        away_goals = _score_int(away.get("score"))
        if not all((home_name, away_name, match_date)) or home_goals is None or away_goals is None:
            continue
        match_id = str(event.get("id") or "")
        identity = (match_id or str(match_date), str(home_name), str(away_name), home_goals, away_goals)
        if identity in seen:
            continue
        seen.add(identity)
        rows.append({
            "id": match_id or None,
            "date": str(match_date),
            "home": str(home_name),
            "away": str(away_name),
            "homeGoals": home_goals,
            "awayGoals": away_goals,
        })
    return rows


def poisson_match_model(
    home_history: list[dict],
    away_history: list[dict],
    league_home_avg: float = 1.45,
    league_away_avg: float = 1.20,
) -> dict:
    """Estimate 1X2, over/under 2.5 and BTTS using recency-weighted home/away rates."""
    import math

    def valid_rows(rows: Any) -> list[dict]:
        return [
            row for row in rows if isinstance(row, dict)
            and _score_int(row.get("gf")) is not None
            and _score_int(row.get("ga")) is not None
        ] if isinstance(rows, list) else []

    home_rows = valid_rows(home_history)
    away_rows = valid_rows(away_history)
    league_home_avg = max(0.5, min(3.5, float(league_home_avg)))
    league_away_avg = max(0.4, min(3.0, float(league_away_avg)))

    def weighted(rows: list[dict], field: str, default: float) -> float:
        if not rows:
            return default
        total = 0.0
        weight_sum = 0.0
        for age, row in enumerate(reversed(rows[-10:])):
            value = _score_int(row.get(field))
            if value is None:
                continue
            weight = 0.82 ** age
            total += value * weight
            weight_sum += weight
        return total / weight_sum if weight_sum else default

    def rate(rows: list[dict], field: str, home_venue: bool, baseline: float) -> float:
        overall = weighted(rows, field, baseline)
        venue_rows = [row for row in rows if row.get("teamIsHome") is home_venue]
        venue_rate = weighted(venue_rows, field, overall) if len(venue_rows) >= 2 else overall
        blended = 0.65 * venue_rate + 0.35 * overall
        return max(0.15, 0.75 * blended + 0.25 * baseline)

    home_attack = rate(home_rows, "gf", True, league_home_avg)
    home_defence = rate(home_rows, "ga", True, league_away_avg)
    away_attack = rate(away_rows, "gf", False, league_away_avg)
    away_defence = rate(away_rows, "ga", False, league_home_avg)

    lambda_home = max(0.15, min(4.0, league_home_avg * (home_attack / league_home_avg) ** 0.55 * (away_defence / league_home_avg) ** 0.45))
    lambda_away = max(0.15, min(4.0, league_away_avg * (away_attack / league_away_avg) ** 0.55 * (home_defence / league_away_avg) ** 0.45))

    def poisson(lam: float, goals: int) -> float:
        return math.exp(-lam) * lam ** goals / math.factorial(goals)

    outcomes = []
    total = p_home = p_draw = p_away = over25 = btts = 0.0
    for hg in range(13):
        for ag in range(13):
            probability = poisson(lambda_home, hg) * poisson(lambda_away, ag)
            outcomes.append((probability, hg, ag))
            total += probability
            if hg > ag:
                p_home += probability
            elif hg == ag:
                p_draw += probability
            else:
                p_away += probability
            if hg + ag >= 3:
                over25 += probability
            if hg > 0 and ag > 0:
                btts += probability
    total = total or 1.0
    return {
        "model": "Recency-weighted Poisson-Modell (Heim-/Auswärtsleistung, zur Liga gemittelt)",
        "probabilities": {
            "home": round(p_home / total * 100, 1),
            "draw": round(p_draw / total * 100, 1),
            "away": round(p_away / total * 100, 1),
        },
        "homeGoals": round(lambda_home, 2),
        "awayGoals": round(lambda_away, 2),
        "underOver": "Über 2,5" if over25 / total >= 0.5 else "Unter 2,5",
        "markets": {
            "over25": round(over25 / total * 100, 1),
            "under25": round((1 - over25 / total) * 100, 1),
            "bttsYes": round(btts / total * 100, 1),
            "bttsNo": round((1 - btts / total) * 100, 1),
        },
        "mostLikelyScore": "%s:%s" % max(outcomes, key=lambda item: item[0])[1:],
        "historyMatches": {"home": len(home_rows), "away": len(away_rows)},
    }

