import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_data import EXPECTED, validate


class ValidateDataTests(unittest.TestCase):
    def fixture(self, league, index, date=None):
        kickoff = date or (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).isoformat()
        return {
            "id": f"test-game-{index}",
            "date": kickoff,
            "league": league,
            "home": f"Home Team {index}",
            "away": f"Away Team {index}",
            "source": "ESPN",
            "analysisStatus": "model_estimate",
            "prediction": {
                "probabilities": {"home": 45.0, "draw": 28.0, "away": 27.0},
                "homeGoals": 1.4,
                "awayGoals": 1.1,
                "markets": {"over25": 48.0, "under25": 52.0, "bttsYes": 50.0, "bttsNo": 50.0},
            },
            "dataCoverage": {"fixture": "ESPN", "prediction": "model estimate"},
        }

    def dataset(self, fixtures=None):
        games = fixtures if fixtures is not None else [
            self.fixture(league, index) for index, league in enumerate(EXPECTED)
        ]
        return {
            "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "status": "free_mode",
            "configuredLeagues": EXPECTED,
            "fixtureWindowDays": 14,
            "recommendations": games,
            "errors": [],
            "sourceDiagnostics": {
                "OpenFootball": {"status": "ok", "httpSuccess": True, "fixtures": 5},
                "ESPN": {"status": "ok", "httpSuccess": True, "fixtures": 10},
                "OpenLigaDB": {"status": "empty", "httpSuccess": True, "fixtures": 0},
                "SportScore": {"status": "ok", "httpSuccess": True, "fixtures": 3},
                "football-data.org": {"status": "not_configured", "httpSuccess": None, "fixtures": 0},
                "API-Football": {"status": "not_configured", "httpSuccess": None, "fixtures": 0},
            },
        }

    def run_validation(self, data, require_leagues=True):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "recommendations.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return validate(path, ROOT / "competitions.json", require_leagues)

    def test_accepts_valid_full_ten_league_dataset(self):
        self.assertEqual(self.run_validation(self.dataset()), [])

    def test_rejects_missing_competition_coverage(self):
        data = self.dataset([self.fixture(league, 1) for league in EXPECTED[:-1]])
        self.assertTrue(any("Missing fixture coverage" in error for error in self.run_validation(data)))

    def test_rejects_duplicate_normalized_fixture(self):
        first = self.fixture("Bundesliga", 1)
        second = self.fixture("Bundesliga", 2)
        second["home"] = first["home"].replace(" ", ".")
        second["away"] = first["away"].replace(" ", ".")
        second["date"] = first["date"]
        self.assertTrue(any("Duplicate fixture" in error for error in self.run_validation(self.dataset([first, second]), False)))

    def test_rejects_timezone_naive_kickoff(self):
        data = self.dataset([self.fixture("Bundesliga", 1, "2026-10-29T19:00:00")])
        self.assertTrue(any("invalid kickoff timestamp or timezone" in error for error in self.run_validation(data, False)))

    def test_rejects_empty_source_response_without_http_success(self):
        data = self.dataset()
        data["sourceDiagnostics"]["OpenLigaDB"] = {"status": "empty", "httpSuccess": False, "fixtures": 0}
        self.assertTrue(any("empty without a successful HTTP response" in error for error in self.run_validation(data)))

    def test_rejects_source_failure_even_if_other_feeds_have_fixtures(self):
        data = self.dataset()
        data["sourceDiagnostics"]["ESPN"] = {
            "status": "error", "httpSuccess": False, "fixtures": 0, "error": "HTTP 503"
        }
        self.assertTrue(any("Source failed during run: ESPN" in error for error in self.run_validation(data)))

    def test_rejects_empty_dataset(self):
        data = self.dataset([])
        self.assertTrue(any("empty or missing fixture list" in error for error in self.run_validation(data)))

    def test_rejects_invalid_probability_distribution(self):
        data = self.dataset()
        data["recommendations"][0]["prediction"]["probabilities"] = {"home": 70.0, "draw": 28.0, "away": 27.0}
        self.assertTrue(any("probabilities do not sum" in error for error in self.run_validation(data)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
