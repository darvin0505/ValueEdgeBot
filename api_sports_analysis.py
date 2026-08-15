"""Free API-Sports confirmation layer for ValueEdgeBot.

The free plan is protected with date/fixture caches. External information only
makes small confidence adjustments; it can never manufacture a 90% pick.
"""

from __future__ import annotations

import os
import re
import time
import unicodedata
from collections import defaultdict
from datetime import datetime

import requests

API_KEY = os.getenv("API_SPORTS_KEY")
BASES = {
    "football": "https://v3.football.api-sports.io",
    "baseball": "https://v1.baseball.api-sports.io",
    "basketball": "https://v2.nba.api-sports.io",
}
_CACHE = {}
_TTL = 6 * 60 * 60


def _norm(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(c for c in value if not unicodedata.combining(c))
    aliases = {"la clippers": "los angeles clippers", "ny knicks": "new york knicks"}
    clean = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return aliases.get(clean, clean)


def _get(sport, path, params, ttl=_TTL):
    if not API_KEY:
        return []
    key = (sport, path, tuple(sorted((str(k), str(v)) for k, v in params.items())))
    cached = _CACHE.get(key)
    if cached and time.time() - cached[0] < ttl:
        return cached[1]
    try:
        response = requests.get(
            BASES[sport] + path,
            params=params,
            headers={"x-apisports-key": API_KEY},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            return []
        rows = payload.get("response", [])
        _CACHE[key] = (time.time(), rows)
        return rows
    except (requests.RequestException, ValueError) as error:
        print(f"API-SPORTS {sport} ERROR: {error}")
        return []


def _date(event):
    return str(event.get("date") or event.get("commence_time") or "")[:10]


def _teams(event):
    return (
        event.get("home") or event.get("home_team") or "",
        event.get("away") or event.get("away_team") or "",
    )


def _daily_games(sport, date):
    path = "/fixtures" if sport == "football" else "/games"
    return _get(sport, path, {"date": date})


def _game_teams(sport, row):
    teams = row.get("teams", {})
    return teams.get("home", {}).get("name", ""), teams.get("away", {}).get("name", "")


def _same_team(left, right):
    left, right = _norm(left), _norm(right)
    if left == right or left in right or right in left:
        return True
    ignored = {"fc", "cf", "club", "deportivo", "basketball", "baseball"}
    left_tokens = set(left.split()) - ignored
    right_tokens = set(right.split()) - ignored
    return bool(left_tokens and right_tokens and left_tokens == right_tokens)


def _match(sport, event):
    home, away = map(_norm, _teams(event))
    for row in _daily_games(sport, _date(event)):
        ext_home, ext_away = map(_norm, _game_teams(sport, row))
        if _same_team(home, ext_home) and _same_team(away, ext_away):
            return row
        # Some feeds reverse neutral-site labels; preserve the event's orientation.
        if _same_team(home, ext_away) and _same_team(away, ext_home):
            return row
    return None


def _soccer_prediction(row, event):
    fixture_id = row.get("fixture", {}).get("id")
    predictions = _get("football", "/predictions", {"fixture": fixture_id}, ttl=60 * 60)
    if not predictions:
        return {"verified": True, "source": "API-Sports"}
    data = predictions[0]
    prediction = data.get("predictions", {})
    percent = prediction.get("percent", {})
    home_name, away_name = _teams(event)
    values = {
        home_name: _percent(percent.get("home")),
        "Draw": _percent(percent.get("draw")),
        away_name: _percent(percent.get("away")),
    }
    predicted = max(values, key=values.get) if any(values.values()) else None
    return {
        "verified": True,
        "source": "API-Sports Predictions",
        "predicted": predicted,
        "external_probability": values.get(predicted, 0),
        "under_over": prediction.get("under_over"),
        "advice": prediction.get("advice"),
    }


def _percent(value):
    try:
        return float(str(value).replace("%", "")) / 100
    except (TypeError, ValueError):
        return 0.0


def enrich_events(events, sport):
    """Attach independent confirmation without spending requests repeatedly."""
    if not API_KEY:
        return events
    for event in events:
        row = _match(sport, event)
        if not row:
            event["_external_analysis"] = {"verified": False, "source": "API-Sports"}
            continue
        if sport == "football":
            analysis = _soccer_prediction(row, event)
            analysis["external_id"] = row.get("fixture", {}).get("id")
        else:
            analysis = {
                "verified": True,
                "source": "API-Sports Baseball" if sport == "baseball" else "API-Sports NBA",
                "external_id": row.get("id"),
            }
        event["_external_analysis"] = analysis
    return events


def confidence_adjustment(event, selected_outcome):
    """At most +/-3 points: market data remains the anchor."""
    data = event.get("_external_analysis", {})
    if not data.get("verified"):
        return -0.01
    prediction = data.get("predicted")
    if not prediction:
        return 0.005
    return 0.03 if _norm(prediction) == _norm(selected_outcome) else -0.03


def source_text(event):
    market_source = (
        "SportsGameOdds"
        if event.get("_source") == "sportsgameodds"
        else "Odds-API.io"
    )
    data = event.get("_external_analysis", {})
    if not data:
        return market_source
    return market_source + " + " + data.get("source", "API-Sports")


def agreement_text(event, selected_outcome):
    data = event.get("_external_analysis", {})
    if not data.get("verified"):
        return "⚠️ Sin confirmación externa"
    prediction = data.get("predicted")
    if not prediction:
        return "✅ Evento confirmado por segunda fuente"
    if _norm(prediction) == _norm(selected_outcome):
        return "✅ Las fuentes coinciden"
    return "⚠️ Las fuentes discrepan"
