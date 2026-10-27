import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from feed_utils import espn_completed_match_rows, full_time_score, match_rows, normalize_team_name, parse_openfootball_cup, poisson_match_model, sportscore_match_rows


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
                "DFB-Pokal",
            ],
        )
        self.assertEqual(len({item["name"] for item in config["competitions"]}), 11)
        self.assertTrue(all(item.get("espnSlug") for item in config["competitions"]))


class EspnCompletedMatchTests(unittest.TestCase):
    def test_extracts_only_completed_matches_with_two_scores(self):
        events = [
            {
                "id": "finished-1",
                "date": "2026-10-24T16:00:00Z",
                "status": {"type": {"completed": True, "state": "post"}},
                "competitions": [{"competitors": [
                    {"homeAway": "home", "team": {"displayName": "Home FC"}, "score": "2"},
                    {"homeAway": "away", "team": {"displayName": "Away FC"}, "score": "1"},
                ]}],
            },
            {
                "id": "upcoming-1",
                "date": "2026-10-29T16:00:00Z",
                "status": {"type": {"completed": False, "state": "pre"}},
                "competitions": [{"competitors": [
                    {"homeAway": "home", "team": {"displayName": "Home FC"}, "score": ""},
                    {"homeAway": "away", "team": {"displayName": "Away FC"}, "score": ""},
                ]}],
            },
        ]
        self.assertEqual(espn_completed_match_rows(events), [{
            "id": "finished-1",
            "date": "2026-10-24T16:00:00Z",
            "home": "Home FC",
            "away": "Away FC",
            "homeGoals": 2,
            "awayGoals": 1,
        }])

    def test_ignores_malformed_payloads(self):
        self.assertEqual(espn_completed_match_rows(None), [])
        self.assertEqual(espn_completed_match_rows([None, {}, {"competitions": []}]), [])


class PoissonModelTests(unittest.TestCase):
    def test_probabilities_and_markets_are_valid(self):
        history = [
            {"date": "2026-10-01", "gf": 2, "ga": 0, "teamIsHome": True},
            {"date": "2026-10-05", "gf": 1, "ga": 1, "teamIsHome": False},
            {"date": "2026-10-10", "gf": 3, "ga": 1, "teamIsHome": True},
            {"date": "2026-10-15", "gf": 2, "ga": 2, "teamIsHome": False},
            {"date": "2026-10-20", "gf": 2, "ga": 1, "teamIsHome": True},
        ]
        result = poisson_match_model(history, list(reversed(history)))
        probabilities = result["probabilities"]
        self.assertAlmostEqual(sum(probabilities.values()), 100.0, delta=0.2)
        self.assertGreaterEqual(min(probabilities.values()), 0.0)
        self.assertLessEqual(max(probabilities.values()), 100.0)
        self.assertAlmostEqual(result["markets"]["over25"] + result["markets"]["under25"], 100.0, delta=0.2)
        self.assertAlmostEqual(result["markets"]["bttsYes"] + result["markets"]["bttsNo"], 100.0, delta=0.2)
        self.assertRegex(result["mostLikelyScore"], r"^\d+:\d+$")

    def test_empty_history_returns_finite_baseline(self):
        result = poisson_match_model([], [])
        self.assertGreater(result["homeGoals"], 0)
        self.assertGreater(result["awayGoals"], 0)
        self.assertAlmostEqual(sum(result["probabilities"].values()), 100.0, delta=0.2)
        self.assertEqual(result["historyMatches"], {"home": 0, "away": 0})

    def test_low_scoring_h2h_reduces_goal_expectation_despite_high_recent_form(self):
        home_form = [
            {"gf": 4, "ga": 1, "teamIsHome": True},
            {"gf": 3, "ga": 1, "teamIsHome": False},
            {"gf": 5, "ga": 2, "teamIsHome": True},
            {"gf": 4, "ga": 0, "teamIsHome": False},
            {"gf": 3, "ga": 1, "teamIsHome": True},
        ]
        away_form = [
            {"gf": 4, "ga": 1, "teamIsHome": False},
            {"gf": 3, "ga": 0, "teamIsHome": True},
            {"gf": 4, "ga": 2, "teamIsHome": False},
            {"gf": 5, "ga": 1, "teamIsHome": True},
            {"gf": 3, "ga": 1, "teamIsHome": False},
        ]
        h2h = [
            {"gf": 0, "ga": 0, "teamIsHome": True},
            {"gf": 1, "ga": 0, "teamIsHome": False},
            {"gf": 1, "ga": 1, "teamIsHome": True},
            {"gf": 0, "ga": 1, "teamIsHome": False},
            {"gf": 1, "ga": 0, "teamIsHome": True},
        ]
        form_only = poisson_match_model(home_form, away_form)
        with_h2h = poisson_match_model(home_form, away_form, h2h_history=h2h)
        self.assertLess(with_h2h["homeGoals"] + with_h2h["awayGoals"], form_only["homeGoals"] + form_only["awayGoals"])
        self.assertEqual(with_h2h["h2h"]["matchesUsed"], 5)
        self.assertEqual(with_h2h["h2h"]["averageTotalGoals"], 1.0)
        self.assertTrue(with_h2h["h2h"]["lowScoringPattern"])
        self.assertGreater(with_h2h["h2h"]["modelWeightPercent"], 0)


class SportScoreFixtureTests(unittest.TestCase):
    def test_parses_documented_widget_style_match(self):
        payload = {"sport": "football", "count": 1, "matches": [{
            "slug": "home-fc-vs-away-fc",
            "competition": {"name": "Bundesliga"},
            "home": "Home FC",
            "away": "Away FC",
            "status": "finished",
            "time": "2026-10-24T14:30:00Z",
            "score": "2-1",
            "venue": {"name": "Home Stadium"},
        }]}
        rows = sportscore_match_rows(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["home"], "Home FC")
        self.assertEqual(rows[0]["away"], "Away FC")
        self.assertEqual(rows[0]["competition"], "Bundesliga")
        self.assertEqual((rows[0]["homeGoals"], rows[0]["awayGoals"]), (2, 1))
        self.assertEqual(rows[0]["venue"], "Home Stadium")

    def test_parses_v1_fixture_variants_and_deduplicates(self):
        match = {
            "fixtureId": 17,
            "competition_name": "uefa-champions-league",
            "homeTeam": {"name": "Team A"},
            "away_team": {"displayName": "Team B"},
            "utcDate": "2026-11-03T18:45:00Z",
            "status": {"name": "FINISHED"},
            "scores": {"fullTime": {"home": 0, "away": 0}},
        }
        payload = {"data": {"fixtures": [match, match, None]}}
        rows = sportscore_match_rows(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "17")
        self.assertEqual(rows[0]["competition"], "uefa-champions-league")
        self.assertEqual((rows[0]["homeGoals"], rows[0]["awayGoals"]), (0, 0))
        self.assertEqual(rows[0]["status"], "FINISHED")


    def test_combines_date_and_clock_without_losing_timezone(self):
        row = {
            "id": "match-19",
            "competitionSlug": "turkish-super-lig",
            "home": {"name": "Galatasaray"},
            "away": {"name": "Fenerbahce"},
            "date": "2026-10-31",
            "time": "19:00",
            "timeZone": "Europe/Istanbul",
            "status": "upcoming",
        }
        parsed = sportscore_match_rows({"fixtures": [row]})[0]
        self.assertEqual(parsed["date"], "2026-10-31T19:00:00")
        self.assertEqual(parsed["timezone"], "Europe/Istanbul")
        self.assertEqual(parsed["competition"], "turkish-super-lig")

    def test_ignores_unusable_payloads(self):
        self.assertEqual(sportscore_match_rows(None), [])
        self.assertEqual(sportscore_match_rows({"data": "not-a-list"}), [])
        self.assertEqual(sportscore_match_rows([{"home": "A"}]), [])



class HistoricalH2HRegressionTests(unittest.TestCase):
    def test_canonical_team_names_join_across_public_feed_aliases(self):
        self.assertEqual(normalize_team_name("Rot-Weiss Essen"), "rotweissessen")
        self.assertEqual(normalize_team_name("Rot Weiss Essen e.V."), "rotweissessen")
        self.assertEqual(normalize_team_name("RasenBallsport Leipzig"), "rbleipzig")
        self.assertEqual(normalize_team_name("RasenBallsport Leipzig GmbH"), "rbleipzig")
        self.assertEqual(normalize_team_name("RB Leipzig"), "rbleipzig")

    def test_rot_weiss_essen_rb_leipzig_confirmed_2024_pokal_result(self):
        # Official DFB Datencenter and RWE match report: 17 Aug 2024, 1-4.
        history = {
            normalize_team_name("Rot-Weiss Essen"): [{
                "date": "2024-08-17T13:30:00+00:00",
                "gf": 1, "ga": 4, "result": "L",
                "opponent": "RasenBallsport Leipzig GmbH",
                "home": "Rot-Weiss Essen", "away": "RB Leipzig",
                "homeGoals": 1, "awayGoals": 4, "teamIsHome": True,
                "historySource": "verified-regression-fixture",
            }]
        }
        rows = [
            row for team_key, matches in history.items()
            if team_key == normalize_team_name("Rot-Weiss Essen")
            for row in matches
            if normalize_team_name(row["opponent"]) == normalize_team_name("RB Leipzig")
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["homeGoals"], rows[0]["awayGoals"]), (1, 4))
        self.assertEqual(rows[0]["date"][:10], "2024-08-17")
        model = poisson_match_model([], [], h2h_history=[{
            "gf": rows[0]["gf"], "ga": rows[0]["ga"], "teamIsHome": True
        }])
        self.assertEqual(model["h2h"]["matchesUsed"], 1)
        self.assertEqual(model["h2h"]["averageTotalGoals"], 5.0)




class OpenFootballCupParserTests(unittest.TestCase):
    def test_parses_confirmed_rwe_leipzig_and_ignores_penalty_aggregate(self):
        source = """
▪ Round 1
  Sat Aug 17
           Rot-Weiss Essen         v RB Leipzig               1-4 (1-2)
           Würzburger Kickers      v 1899 Hoffenheim          3-5 pen. 2-2 a.e.t. (1-1, 1-1)
           SV Wehen Wiesbaden      v 1. FSV Mainz 05          1-3 a.e.t. (1-1, 1-0)
"""
        rows = parse_openfootball_cup(source, 2024, today="2024-12-31")
        self.assertEqual(len(rows), 2)
        rwe = next(row for row in rows if row["home"] == "Rot-Weiss Essen")
        self.assertEqual((rwe["homeGoals"], rwe["awayGoals"]), (1, 4))
        self.assertEqual(rwe["date"], "2024-08-17")
        aet = next(row for row in rows if row["home"] == "SV Wehen Wiesbaden")
        self.assertEqual((aet["homeGoals"], aet["awayGoals"]), (1, 3))

    def test_skips_future_matches_and_duplicate_rows(self):
        source = """
  Sat Aug 17
           Rot-Weiss Essen         v RB Leipzig               1-4 (1-2)
           Rot-Weiss Essen         v RB Leipzig               1-4 (1-2)
  Tue Oct 27
           Rot-Weiss Essen         v RB Leipzig               0-0
"""
        rows = parse_openfootball_cup(source, 2026, today="2026-10-27")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2026-08-17")

if __name__ == "__main__":
    unittest.main(verbosity=2)
