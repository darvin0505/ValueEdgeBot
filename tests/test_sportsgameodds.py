import unittest
from unittest.mock import patch

import sportsgameodds


class SportsGameOddsAdapterTests(unittest.TestCase):
    def sample_event(self):
        return {
            "eventID": "opaque-event-id",
            "leagueID": "MLB",
            "teams": {
                "home": {"names": {"long": "Yankees"}},
                "away": {"names": {"long": "Mets"}},
            },
            "status": {"startsAt": "2026-08-15T23:05:00Z"},
            "players": {
                "AARON_JUDGE_1_MLB": {"name": "Aaron Judge", "teamID": "NYY_MLB"}
            },
            "odds": {
                "points-home-game-ml-home": {
                    "periodID": "game", "betTypeID": "ml", "sideID": "home",
                    "statEntityID": "home", "statID": "points",
                    "byBookmaker": {"draftkings": {"available": True, "odds": "-130"}},
                },
                "points-away-game-ml-away": {
                    "periodID": "game", "betTypeID": "ml", "sideID": "away",
                    "statEntityID": "away", "statID": "points",
                    "byBookmaker": {"draftkings": {"available": True, "odds": "+115"}},
                },
                "batting_hits-AARON_JUDGE_1_MLB-game-ou-over": {
                    "periodID": "game", "betTypeID": "ou", "sideID": "over",
                    "statEntityID": "AARON_JUDGE_1_MLB", "statID": "batting_hits",
                    "byBookmaker": {"fanduel": {"available": True, "odds": "-110", "overUnder": "1.5"}},
                },
            },
        }

    def test_normalizes_event_moneyline_and_player_prop(self):
        event = sportsgameodds.normalize_event(self.sample_event(), "baseball")
        self.assertEqual(event["id"], "opaque-event-id")
        self.assertEqual(event["home"], "Yankees")
        self.assertEqual(event["_source"], "sportsgameodds")
        dk = event["_odds"]["bookmakers"]["Draftkings"]
        ml = next(m for m in dk if m["name"] == "ML")
        self.assertEqual(ml["odds"][0]["home"], "-130")
        fd = event["_odds"]["bookmakers"]["Fanduel"]
        prop = next(m for m in fd if m["name"] == "Player Hits")
        self.assertEqual(prop["odds"][0]["player"], "Aaron Judge")
        self.assertEqual(prop["odds"][0]["point"], "1.5")

    @patch("sportsgameodds._request")
    def test_get_score_requires_final_status(self, request):
        request.return_value = [{"status": {"finalized": True}, "scores": {"home": 5, "away": 4}}]
        self.assertEqual(
            sportsgameodds.get_score("opaque-event-id"),
            {"home": 5, "away": 4, "status": "settled"},
        )


if __name__ == "__main__":
    unittest.main()
