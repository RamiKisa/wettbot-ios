import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from feed_utils import full_time_score, match_rows


class MatchRowsTests(unittest.TestCase):
    def test_openfootball_object_shape(self):
        rows = [{"date": "2026-10-30", "team1": "A", "team2": "B"}]
        self.assertEqual(match_rows({"name": "fixture feed", "matches": rows}), rows)

    def test_top_level_list_shape(self):
        rows = [{"date": "2026-10-30", "team1": "A", "team2": "B"}]
        self.assertEqual(match_rows(rows), rows)

    def test_ignores_non_object_rows(self):
        self.assertEqual(match_rows({"matches": [None, ["bad"], {"date": "2026-10-30"}]}), [{"date": "2026-10-30"}])

    def test_invalid_payload_is_empty(self):
        self.assertEqual(match_rows(None), [])
        self.assertEqual(match_rows({"matches": "not-a-list"}), [])


class FullTimeScoreTests(unittest.TestCase):
    def test_nested_fulltime_list(self):
        self.assertEqual(full_time_score({"ht": [1, 0], "ft": [2, 1]}), (2, 1))

    def test_nested_fulltime_object(self):
        self.assertEqual(full_time_score({"ft": {"team1": 0, "team2": 0}}), (0, 0))

    def test_top_level_score_pair(self):
        self.assertEqual(full_time_score([0, 0]), (0, 0))

    def test_direct_home_away_score_pair(self):
        self.assertEqual(full_time_score({"home": 3, "away": 2}), (3, 2))

    def test_missing_or_malformed_score(self):
        self.assertEqual(full_time_score(None), (None, None))
        self.assertEqual(full_time_score([]), (None, None))
        self.assertEqual(full_time_score({"ht": [1, 0]}), (None, None))


class FixedCompetitionConfigTests(unittest.TestCase):
    def test_exact_fixed_competition_list(self):
        config = json.loads((ROOT / "competitions.json").read_text(encoding="utf-8"))
        self.assertEqual(config["fixtureWindowDays"], 14)
        self.assertEqual(
            [item["name"] for item in config["competitions"]],
            [
                "Bundesliga", "Serie A", "Ligue 1", "La Liga", "Premier League",
                "Süper Lig", "Primeira Liga", "Champions League", "Europa League",
                "Conference League",
            ],
        )
        self.assertEqual(len({item["name"] for item in config["competitions"]}), 10)
        self.assertTrue(all(item.get("espnSlug") for item in config["competitions"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
