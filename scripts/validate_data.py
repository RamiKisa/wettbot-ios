#!/usr/bin/env python3
"""Validate RamoBet's generated fixture dataset before it can be published."""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import pathlib
import re
import sys
import unicodedata
from zoneinfo import ZoneInfo

EXPECTED = [
    "Bundesliga", "Serie A", "Ligue 1", "La Liga", "Premier League",
    "Süper Lig", "Primeira Liga", "Champions League", "Europa League",
    "Conference League",
]


def norm(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()
    key = re.sub(r"[^a-z0-9]", "", text)
    aliases = {
        "sportlisboabenfica": "benfica", "sportlisboaebenfica": "benfica",
        "fcarouca": "arouca", "arouca": "arouca", "cdnacional": "nacional", "nacional": "nacional",
        "sportingclubedebraga": "braga", "braga": "braga", "csmaritimo": "maritimo", "maritimo": "maritimo",
        "omonianicosiafc": "omonianicosia", "omonianicosia": "omonianicosia",
        
        "sportlisboaebenficafutebolclube": "benfica", "slbenfica": "benfica",
        "benficasl": "benfica", "benfica": "benfica",
        "sportlisboabenficafutebol": "benfica",
        "cdsantaclara": "santaclara", "santaclara": "santaclara",
        "santaclaracd": "santaclara",
        "fcbarcelona": "barcelona", "barcelonafc": "barcelona",
        "bayernmunchen": "bayernmunich", "fcbayernmunchen": "bayernmunich",
        "fcbayern": "bayernmunich", "parissaintgermain": "psg",
        "parissaintgermainfc": "psg", "fcporto": "porto",
        "sportingcp": "sportinglisbon", "sportingclubedeportugal": "sportinglisbon",
        "sportinglisbon": "sportinglisbon",
    }
    return aliases.get(key, key)


def parse_datetime(value: object) -> dt.datetime:
    stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("timezone offset is required")
    return stamp.astimezone(dt.timezone.utc)


def validate(data_path: pathlib.Path, config_path: pathlib.Path, require_leagues: bool) -> list[str]:
    errors: list[str] = []
    config = json.loads(config_path.read_text(encoding="utf-8"))
    names = [item.get("name") for item in config.get("competitions", []) if isinstance(item, dict)]
    if names != EXPECTED:
        errors.append(f"Fixed competition list mismatch: {names!r}")
    if config.get("fixtureWindowDays") != 14:
        errors.append("fixtureWindowDays must remain exactly 14")
    if any(not item.get("espnSlug") for item in config.get("competitions", []) if isinstance(item, dict)):
        errors.append("Every configured competition must retain an ESPN slug")
    if any(not item.get("apiFootballId") for item in config.get("competitions", []) if isinstance(item, dict)):
        errors.append("Every configured competition must retain an API-Football league mapping")
    if any(item.get("openFootballFile") and not item.get("openFootballTimezone")
           for item in config.get("competitions", []) if isinstance(item, dict)):
        errors.append("OpenFootball feeds must have explicit timezone metadata")

    data = json.loads(data_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return errors + ["Generated data must be a JSON object"]
    fixtures = data.get("recommendations")
    if not isinstance(fixtures, list) or not fixtures:
        return errors + ["Refusing to publish an empty or missing fixture list"]
    allowed = set(EXPECTED)
    configured_leagues = data.get("configuredLeagues")
    if configured_leagues != EXPECTED:
        errors.append(f"Generated configuredLeagues mismatch: {configured_leagues!r}")
    if data.get("fixtureWindowDays") != 14:
        errors.append("Generated fixtureWindowDays must remain exactly 14")
    generated = data.get("generatedAt")
    try:
        generated_at = parse_datetime(generated)
    except (TypeError, ValueError):
        generated_at = dt.datetime.now(dt.timezone.utc)
        errors.append(f"Invalid generatedAt timestamp: {generated!r}")
    now = dt.datetime.now(dt.timezone.utc)
    # Refresh outputs timestamp at the beginning of the job. Allow its run time,
    # but reject stale outputs and fixtures outside the configured future window.
    if now - generated_at > dt.timedelta(hours=8):
        errors.append(f"Generated dataset is stale: generatedAt={generated}")
    end = generated_at + dt.timedelta(days=14, minutes=5)

    ids: set[str] = set()
    duplicate_keys: set[tuple] = set()
    by_league: collections.Counter[str] = collections.Counter()
    by_source: collections.Counter[str] = collections.Counter()
    by_analysis: collections.Counter[str] = collections.Counter()
    source_status = data.get("sourceDiagnostics")
    if not isinstance(source_status, dict):
        errors.append("Missing sourceDiagnostics; cannot distinguish failed sources from empty successful feeds")
    else:
        for source in ("OpenFootball", "ESPN", "OpenLigaDB", "SportScore", "football-data.org", "API-Football"):
            item = source_status.get(source)
            if not isinstance(item, dict) or "status" not in item or "httpSuccess" not in item:
                errors.append(f"Missing HTTP/source diagnostics for {source}")
                continue
            status = item.get("status")
            if status not in {"ok", "empty", "not_configured", "error"}:
                errors.append(f"Invalid diagnostic status for {source}: {status!r}")
            if status == "error":
                errors.append(f"Source failed during run: {source}: {item.get('error') or 'no error detail'}")
            if status == "empty" and item.get("httpSuccess") is not True:
                errors.append(f"{source} reported empty without a successful HTTP response")
            if status == "ok" and int(item.get("fixtures", 0)) < 0:
                errors.append(f"{source} reported invalid fixture count")
    for i, item in enumerate(fixtures):
        if not isinstance(item, dict):
            errors.append(f"fixture[{i}] is not an object")
            continue
        missing = [key for key in ("id", "date", "league", "home", "away", "source") if not item.get(key)]
        for key in missing:
            errors.append(f"fixture[{i}] missing {key}")
        if item.get("league") not in allowed:
            errors.append(f"fixture[{i}] unexpected league: {item.get('league')!r}")
        identifier = str(item.get("id") or "")
        if not identifier:
            pass
        elif identifier in ids:
            errors.append(f"Duplicate fixture id: {identifier}")
        ids.add(identifier)
        league = item.get("league", "UNKNOWN")
        source = item.get("source", "unknown")
        by_league[league] += 1
        by_source[source] += 1
        status = item.get("analysisStatus", "unknown")
        by_analysis[status] += 1
        if status not in {"available", "model_estimate", "limited_model"}:
            errors.append(f"fixture[{i}] not analyzed: analysisStatus={status!r}")
        if not isinstance(item.get("dataCoverage"), dict):
            errors.append(f"fixture[{i}] missing dataCoverage metadata")
        try:
            kickoff = parse_datetime(item.get("date"))
            if kickoff <= now - dt.timedelta(minutes=5):
                errors.append(f"fixture[{i}] is not upcoming: {league} {item.get('home')} - {item.get('away')}")
            if kickoff > end:
                errors.append(f"fixture[{i}] outside 14-day window: {item.get('date')}")
        except (TypeError, ValueError):
            errors.append(f"fixture[{i}] invalid kickoff timestamp or timezone: {item.get('date')!r}")
        try:
            local_day = kickoff.astimezone(ZoneInfo(config.get("timezone", "Europe/Berlin"))).date().isoformat()
        except Exception:
            local_day = str(item.get("date", ""))[:10]
        teams = tuple(sorted((norm(item.get("home")), norm(item.get("away")))))
        key = (league, local_day, teams)
        if key in duplicate_keys:
            errors.append(f"Duplicate fixture (normalized teams/day): {key}")
        duplicate_keys.add(key)
        prediction = item.get("prediction")
        if not isinstance(prediction, dict):
            errors.append(f"fixture[{i}] missing prediction object")
            continue
        probs = prediction.get("probabilities")
        if not isinstance(probs, dict) or any(
            not isinstance(probs.get(k), (int, float)) or not math.isfinite(probs[k])
            for k in ("home", "draw", "away")
        ):
            errors.append(f"fixture[{i}] missing finite 1X2 probabilities")
        elif not 98 <= sum(probs[k] for k in ("home", "draw", "away")) <= 102:
            errors.append(f"fixture[{i}] 1X2 probabilities do not sum to about 100%")
        for k in ("homeGoals", "awayGoals"):
            value = prediction.get(k)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                errors.append(f"fixture[{i}] invalid {k} estimate")
        markets = prediction.get("markets")
        if not isinstance(markets, dict) or any(
            not isinstance(markets.get(k), (int, float)) or not math.isfinite(markets[k])
            for k in ("over25", "under25", "bttsYes", "bttsNo")
        ):
            errors.append(f"fixture[{i}] missing finite O/U and BTTS estimates")

    if require_leagues:
        missing_leagues = [league for league in EXPECTED if by_league[league] == 0]
        if missing_leagues:
            errors.append("Missing fixture coverage for configured leagues: " + ", ".join(missing_leagues))
    if not isinstance(data.get("errors"), list):
        errors.append("Generated data is missing source/error list")
    elif data.get("errors"):
        # A source outage is an actionable warning and must not be hidden by
        # other successful feeds. Treat source errors as release-blocking.
        errors.append("Refresh reported source errors: " + "; ".join(map(str, data["errors"][:12])))

    print(f"Validation summary: fixtures={len(fixtures)} leagues={len(by_league)}")
    print("Fixtures by league: " + json.dumps(dict(sorted(by_league.items())), ensure_ascii=False))
    print("Fixtures by source: " + json.dumps(dict(sorted(by_source.items())), ensure_ascii=False))
    print("Analysis status: " + json.dumps(dict(sorted(by_analysis.items())), ensure_ascii=False))
    print("Source diagnostics: " + json.dumps(source_status, ensure_ascii=False, sort_keys=True))
    if errors:
        print("VALIDATION FAILED:")
        for error in errors[:60]:
            print(" - " + error)
    else:
        print("VALIDATION OK")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("data", nargs="?", default="recommendations.json")
    parser.add_argument("--config", default="competitions.json")
    parser.add_argument("--require-leagues", action="store_true")
    args = parser.parse_args()
    try:
        errors = validate(pathlib.Path(args.data), pathlib.Path(args.config), args.require_leagues)
    except Exception as exc:
        print(f"VALIDATION FAILED: could not inspect generated dataset: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
