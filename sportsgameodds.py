"""SportsGameOdds v2 adapter for ValueEdgeBot.

The adapter keeps the rest of the bot independent from the provider schema and
caches each league response for ten minutes, matching the Amateur plan's update
frequency while protecting its monthly object allowance.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

BASE_URL = "https://api.sportsgameodds.com/v2"
LEAGUES = {
    "baseball": ("MLB",),
    "basketball": ("NBA",),
    # These are the soccer leagues listed on the free Amateur plan.
    "football": ("MLS", "UEFA_CHAMPIONS_LEAGUE"),
}

LEAGUE_NAMES = {
    "MLB": "MLB",
    "NBA": "NBA",
    "MLS": "MLS",
    "UEFA_CHAMPIONS_LEAGUE": "Champions League",
    "CHAMPIONS_LEAGUE": "Champions League",
    "UCL": "Champions League",
}

PROP_NAMES = {
    "batting_hits": "Player Hits",
    "hits": "Player Hits",
    "hits_runs_rbis": "Player Hits + Runs + RBI",
    "hits_runs_rbi": "Player Hits + Runs + RBI",
    "pitching_strikeouts": "Player Strikeouts",
    "strikeouts": "Player Strikeouts",
    "points": "Player Points",
    "rebounds": "Player Rebounds",
    "assists": "Player Assists",
    "threes_made": "Player Threes Made",
}

_CACHE: dict[str, tuple[float, list[dict]]] = {}


def configured() -> bool:
    return bool(os.getenv("SPORTSGAMEODDS_API_KEY"))


def _request(params: dict) -> list[dict] | None:
    api_key = os.getenv("SPORTSGAMEODDS_API_KEY")
    if not api_key:
        return None
    try:
        response = requests.get(
            f"{BASE_URL}/events",
            params=params,
            headers={"x-api-key": api_key, "User-Agent": "ValueEdgeBot/3.0"},
            timeout=35,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success", True):
            print(f"SPORTSGAMEODDS ERROR: {payload.get('error', 'respuesta fallida')}")
            return None
        return payload.get("data", [])
    except (requests.RequestException, ValueError) as error:
        print(f"SPORTSGAMEODDS ERROR: {error}")
        return None


def _team_name(team: dict) -> str:
    names = team.get("names", {}) if isinstance(team, dict) else {}
    return names.get("long") or names.get("medium") or team.get("name") or "N/A"


def _player_name(event: dict, player_id: str) -> str:
    player = (event.get("players") or {}).get(player_id, {})
    return player.get("name") or player_id.replace("_", " ").title()


def _price(book_quote: dict, odd: dict):
    return book_quote.get("odds", odd.get("bookOdds", odd.get("fairOdds")))


def _legacy_odds(event: dict) -> dict:
    """Convert SGO odds to the small legacy shape used by ValueEdgeBot."""
    books: dict[str, dict[str, list[dict]]] = {}
    odds = event.get("odds") or {}

    for odd in odds.values():
        if odd.get("periodID") != "game":
            continue
        bet_type = odd.get("betTypeID")
        side = odd.get("sideID")
        entity = odd.get("statEntityID")
        stat_id = odd.get("statID", "")

        for book_id, quote in (odd.get("byBookmaker") or {}).items():
            if not quote.get("available", True):
                continue
            price = _price(quote, odd)
            if price is None:
                continue
            label = book_id.replace("_", " ").title()
            markets = books.setdefault(label, {})

            if bet_type in {"ml", "ml3way"} and side in {"home", "away", "draw"}:
                rows = markets.setdefault("ML", [{}])
                rows[0][side] = price
            elif bet_type == "yn" and stat_id == "bothTeamsScored" and side in {"yes", "no"}:
                rows = markets.setdefault("Both Teams To Score", [{}])
                rows[0][side] = price
            elif bet_type == "sp" and side in {"home", "away"}:
                rows = markets.setdefault("Spread", [])
                rows.append({
                    "name": side.title(),
                    side: side.title(),
                    "hdp": quote.get("spread", odd.get("bookSpread")),
                    "price": price,
                    f"{side}_price": price,
                })
            elif bet_type == "ou" and entity == "all" and side in {"over", "under"}:
                line = quote.get("overUnder", odd.get("bookOverUnder"))
                rows = markets.setdefault("Totals", [])
                row = next((r for r in rows if str(r.get("hdp")) == str(line)), None)
                if row is None:
                    row = {"hdp": line}
                    rows.append(row)
                row[side] = side.title()
                row[f"{side}_price"] = price
            elif bet_type == "ou" and entity not in {"all", "home", "away"}:
                market_name = PROP_NAMES.get(stat_id)
                if not market_name:
                    continue
                line = quote.get("overUnder", odd.get("bookOverUnder"))
                markets.setdefault(market_name, []).append({
                    "player": _player_name(event, entity),
                    "side": side.title(),
                    "point": line,
                    "price": price,
                    "team": (event.get("players") or {}).get(entity, {}).get("teamID"),
                })

    return {
        "id": event.get("eventID"),
        "bookmakers": [
            {"name": name, "markets": [{"name": market, "odds": rows} for market, rows in markets.items()]}
            for name, markets in books.items()
        ],
    }


def normalize_event(event: dict, sport_key: str) -> dict:
    teams = event.get("teams") or {}
    status = event.get("status") or {}
    league_id = event.get("leagueID", "")
    legacy = _legacy_odds(event)
    return {
        "id": event.get("eventID"),
        "home": _team_name(teams.get("home", {})),
        "away": _team_name(teams.get("away", {})),
        "date": status.get("startsAt"),
        "_league": LEAGUE_NAMES.get(league_id, league_id.replace("_", " ").title()),
        "_sport_key": sport_key,
        "_source": "sportsgameodds",
        "_odds": {
            "id": legacy["id"],
            "bookmakers": {
                item["name"]: item["markets"] for item in legacy["bookmakers"]
            },
        },
    }


def get_events(sport_key: str) -> list[dict] | None:
    """Return normalized upcoming events, or None when SGO is unavailable."""
    if not configured():
        return None
    now = time.time()
    cached = _CACHE.get(sport_key)
    cache_seconds = int(os.getenv("SPORTSGAMEODDS_CACHE_SECONDS", "600"))
    if cached and now - cached[0] < cache_seconds:
        return [dict(event) for event in cached[1]]

    ny_tz = ZoneInfo("America/New_York")
    today = datetime.now(ny_tz).date()
    starts_after = datetime.combine(today, datetime.min.time(), ny_tz).astimezone(timezone.utc)
    starts_before = (datetime.combine(today, datetime.min.time(), ny_tz) + timedelta(days=2)).astimezone(timezone.utc)
    data = _request({
        "leagueID": ",".join(LEAGUES[sport_key]),
        "oddsAvailable": "true",
        "finalized": "false",
        "startsAfter": starts_after.isoformat().replace("+00:00", "Z"),
        "startsBefore": starts_before.isoformat().replace("+00:00", "Z"),
        "bookmakerID": os.getenv("SPORTSGAMEODDS_BOOKMAKERS", "draftkings,fanduel"),
        "includeOpposingOdds": "true",
        "limit": 100,
    })
    if data is None:
        return None
    result = [normalize_event(event, sport_key) for event in data]
    _CACHE[sport_key] = (now, result)
    return [dict(event) for event in result]


def get_score(event_id: str) -> dict | None:
    data = _request({"eventID": event_id, "limit": 1})
    if not data:
        return None
    event = data[0]
    status = event.get("status") or {}
    if not (status.get("finalized") or status.get("completed") or status.get("ended")):
        return None
    scores = event.get("scores") or {}
    if scores.get("home") is None or scores.get("away") is None:
        return None
    return {"home": scores["home"], "away": scores["away"], "status": "settled"}
