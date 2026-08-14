"""Statcast-backed MLB player-prop analysis for ValueEdgeBot."""

from __future__ import annotations

import csv
import io
import json
import math
import re
import statistics
import time
import unicodedata
from datetime import datetime

import requests

SAVANT_URL = "https://baseballsavant.mlb.com/leaderboard/custom"
MLB_PEOPLE_URL = "https://statsapi.mlb.com/api/v1/people"
MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
MIN_PROP_PROBABILITY = 0.56
MIN_PROP_SCORE = 60.0
_CACHE = {"season": None, "loaded": 0.0, "batters": {}, "pitchers": {}}
_PREVIEW_CACHE = {}
_SCHEDULE_CACHE = {}


def number(value, default=None):
    try:
        return float(str(value).replace("%", "").replace(",", ""))
    except (TypeError, ValueError):
        return default


def norm(text):
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def implied_probability(price):
    value = number(price)
    if value is None:
        return 0.0
    if 1.01 <= value < 20:  # decimal
        return 1.0 / value
    if value > 0:  # American
        return 100.0 / (value + 100.0)
    return abs(value) / (abs(value) + 100.0)


def display_odds(price):
    value = number(price)
    if value is None:
        return str(price)
    if 1.01 <= value < 20:
        value = round((value - 1) * 100) if value >= 2 else round(-100 / (value - 1))
    else:
        value = round(value)
    return f"+{value}" if value > 0 else str(value)


def _pct(row, *keys):
    for key in keys:
        value = number(row.get(key))
        if value is not None:
            return value / 100 if value > 1 else value
    return None


def _fetch(kind, season):
    response = requests.get(
        SAVANT_URL,
        params={
            "year": season, "type": kind, "filter": "", "sort": "4",
            "sortDir": "desc", "min": "10", "chart": "false", "csv": "true",
            "selections": "pa,k_percent,xba,xwoba,exit_velocity_avg,hard_hit_percent",
        },
        headers={"User-Agent": "ValueEdgeBot/2.0"}, timeout=35,
    )
    response.raise_for_status()
    output = {}
    for row in csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))):
        name = (
            row.get("player_name")
            or row.get("name")
            or row.get("last_name, first_name")
            or ""
        ).strip()
        if "," in name:
            last, first = [part.strip() for part in name.split(",", 1)]
            name = f"{first} {last}"
        if not name:
            name = f"{row.get('first_name', '')} {row.get('last_name', '')}".strip()
        if name:
            output[norm(name)] = {
                "name": name, "pa": number(row.get("pa"), 0),
                "player_id": row.get("player_id"),
                "k_rate": _pct(row, "k_percent", "strikeout_rate"),
                "xba": number(row.get("xba")), "xwoba": number(row.get("xwoba")),
                "hard_hit": _pct(row, "hard_hit_percent", "hardhit_percent"),
            }
    return output


def load_savant(season=None):
    season = season or datetime.now().year
    if _CACHE["season"] == season and time.time() - _CACHE["loaded"] < 21600:
        return _CACHE
    try:
        batters, pitchers = _fetch("batter", season), _fetch("pitcher", season)
    except requests.RequestException as error:
        print(f"STATCAST ERROR: {error}")
        return {"season": season, "batters": {}, "pitchers": {}}
    _CACHE.update(season=season, loaded=time.time(), batters=batters, pitchers=pitchers)
    return _CACHE


def _market_type(name):
    name = norm(name)
    if "home run" in name:
        return None
    if "hit run rbi" in name or "hits runs rbis" in name or "h r rbi" in name:
        return "Hits+Runs+RBI"
    if "strikeout" in name or "pitcher ks" in name or "pitcher k" in name:
        return "Strikeouts"
    if "player hit" in name or name in {"hits", "batter hits"}:
        return "Hits"
    return None


def _markets(event):
    books = event.get("_odds", {}).get("bookmakers", {})
    if isinstance(books, list):
        books = {b.get("name", "Sportsbook"): b.get("markets", []) for b in books}
    for book, markets in books.items():
        if isinstance(markets, dict):
            markets = markets.values()
        for market in markets or []:
            market_name = market.get("name") or market.get("key") or ""
            for original in market.get("odds") or market.get("outcomes") or []:
                label = str(original.get("label") or "")
                kind = _market_type(market_name)
                if norm(market_name) == "player props":
                    statistic = re.findall(r"\(([^()]*)\)", label)
                    kind = _market_type(statistic[-1]) if statistic else None
                if not kind:
                    continue
                if original.get("over") is not None:
                    outcome = dict(original)
                    outcome["price"] = original["over"]
                    outcome["side"] = "Over"
                    if label:
                        player = re.sub(r"\s*\([^()]*\)\s*$", "", label).strip()
                        team_match = re.search(r"\(([^()]*)\)\s*$", player)
                        if team_match:
                            outcome["team"] = team_match.group(1)
                            player = re.sub(r"\s*\([^()]*\)\s*$", "", player).strip()
                        outcome["player"] = player
                    yield book, kind, outcome
                else:
                    yield book, kind, original


def _find(stats, player):
    key = norm(player)
    if key in stats:
        return stats[key]
    tokens = key.split()
    matches = [v for k, v in stats.items() if tokens and tokens[-1] == k.split()[-1] and (tokens[0] == k.split()[0] or len(tokens[0]) == 1 and k.startswith(tokens[0]))]
    return matches[0] if len(matches) == 1 else None


def _line(outcome):
    for key in ("point", "hdp", "line", "total"):
        value = number(outcome.get(key))
        if value is not None:
            return value
    match = re.search(r"(?:over|under|[ou])\s*([0-9]+(?:\.[0-9]+)?)", str(outcome.get("label") or outcome.get("name") or ""), re.I)
    return float(match.group(1)) if match else None


def _poisson_over(mean, line):
    threshold = math.floor(line) + 1
    below = sum(math.exp(-mean) * mean ** k / math.factorial(k) for k in range(threshold))
    return max(0.01, min(0.99, 1 - below))


def _model(kind, line, stat):
    weight = min(1.0, max(0.35, (stat.get("pa") or 0) / 150))
    if kind == "Hits":
        if stat.get("xba") is None:
            return None
        quality = 1 + 0.12 * (((stat.get("hard_hit") or .36) - .36) / .10)
        mean = max(.25, min(2, stat["xba"] * 4.25 * quality))
    elif kind == "Hits+Runs+RBI":
        if stat.get("xwoba") is None or stat.get("xba") is None:
            return None
        mean = max(.5, min(3.5, 4.25 * (.55 * stat["xba"] + .75 * stat["xwoba"])))
    else:
        if stat.get("k_rate") is None:
            return None
        mean = max(1.5, min(10.5, stat["k_rate"] * 24))
    raw = _poisson_over(mean, line)
    return .5 + (raw - .5) * weight


def _current_teams(player_ids):
    ids = sorted({str(value) for value in player_ids if value})
    if not ids:
        return {}
    try:
        response = requests.get(
            MLB_PEOPLE_URL,
            params={"personIds": ",".join(ids), "hydrate": "currentTeam"},
            timeout=25,
            headers={"User-Agent": "ValueEdgeBot/2.0"},
        )
        response.raise_for_status()
        return {
            str(person.get("id")): person.get("currentTeam", {}).get("name")
            for person in response.json().get("people", [])
        }
    except requests.RequestException:
        return {}


def _preview_context(event):
    date = str(event.get("date") or event.get("commence_time") or "")[:10]
    home = event.get("home") or event.get("home_team") or ""
    away = event.get("away") or event.get("away_team") or ""
    cache_key = (date, norm(home), norm(away))
    if cache_key in _PREVIEW_CACHE:
        return _PREVIEW_CACHE[cache_key]
    try:
        if date not in _SCHEDULE_CACHE:
            response = requests.get(
                MLB_SCHEDULE_URL,
                params={"sportId": 1, "date": date, "hydrate": "probablePitcher"},
                timeout=25,
            )
            response.raise_for_status()
            games = [game for day in response.json().get("dates", []) for game in day.get("games", [])]
            _SCHEDULE_CACHE[date] = games
        game = next(
            game for game in _SCHEDULE_CACHE[date]
            if norm(game["teams"]["home"]["team"]["name"]) == norm(home)
            and norm(game["teams"]["away"]["team"]["name"]) == norm(away)
        )
        game_pk = game["gamePk"]
        response = requests.get(
            "https://baseballsavant.mlb.com/preview",
            params={"game_pk": game_pk, "game_date": date[5:7] + "/" + date[8:10] + "/" + date[:4]},
            headers={"User-Agent": "ValueEdgeBot/2.0"}, timeout=35,
        )
        response.raise_for_status()
        match = re.search(r"var teams = (.*?);\s*$", response.text, re.M)
        payload = json.loads(match.group(1)) if match else {}
        players = {}
        for side in ("home", "away"):
            team_data = payload.get(side, {})
            team_name = team_data.get("team", {}).get("name")
            for group in ("hitters", "catchers", "pitchers"):
                for player in team_data.get("roster", {}).get(group, []):
                    name = player.get("person", {}).get("fullName")
                    if name:
                        players[norm(name)] = {
                            "team": team_name,
                            "lineup_confirmed": bool(team_data.get("hasLineup")),
                            "on_bench": bool(player.get("gameStatus", {}).get("isOnBench")),
                            "stats": player,
                        }
        probable_ids = {
            str(game["teams"][side].get("probablePitcher", {}).get("id"))
            for side in ("home", "away")
            if game["teams"][side].get("probablePitcher", {}).get("id")
        }
        context = {"game_pk": game_pk, "players": players, "probable_ids": probable_ids}
    except (requests.RequestException, StopIteration, KeyError, ValueError, json.JSONDecodeError):
        context = {}
    _PREVIEW_CACHE[cache_key] = context
    return context


def analyze_mlb_props(events, season=None):
    savant = load_savant(season)
    if not savant["batters"] or not savant["pitchers"]:
        return []
    candidates = []
    for event in events:
        home = event.get("home") or event.get("home_team") or "N/A"
        away = event.get("away") or event.get("away_team") or "N/A"
        grouped = {}
        for book, kind, outcome in _markets(event):
            player = outcome.get("player") or outcome.get("description") or outcome.get("participant") or outcome.get("player_name")
            side = norm(outcome.get("side") or outcome.get("name") or outcome.get("label"))
            line = _line(outcome)
            price = outcome.get("price", outcome.get("odds"))
            if not player or line is None or not (side.startswith("over") or side == "o") or price is None:
                continue
            item = grouped.setdefault((norm(player), kind, line), {"player": player, "kind": kind, "line": line, "quotes": [], "raw": outcome})
            over_p = implied_probability(price)
            under_price = outcome.get("under")
            under_p = implied_probability(under_price) if under_price is not None else 0
            fair_p = over_p / (over_p + under_p) if under_p > 0 else over_p
            item["quotes"].append((book, price, fair_p))
        preview = _preview_context(event) if grouped else {}
        for item in grouped.values():
            pool = savant["pitchers"] if item["kind"] == "Strikeouts" else savant["batters"]
            stat = _find(pool, item["player"])
            if not stat:
                continue
            preview_player = preview.get("players", {}).get(norm(item["player"]), {})
            preview_stats = preview_player.get("stats", {})
            if preview_stats:
                stat = {
                    **stat,
                    "k_rate": number(preview_stats.get("k_percent"), 0) / 100 if preview_stats.get("k_percent") is not None else stat.get("k_rate"),
                    "xba": number(preview_stats.get("xba"), stat.get("xba")),
                    "xwoba": number(preview_stats.get("xwoba"), stat.get("xwoba")),
                    "hard_hit": number(preview_stats.get("hard_hit_percent"), 0) / 100 if preview_stats.get("hard_hit_percent") is not None else stat.get("hard_hit"),
                }
            model = _model(item["kind"], item["line"], stat)
            if model is None:
                continue
            market_p = statistics.median(q[2] for q in item["quotes"])
            probability = .68 * model + .32 * market_p
            best_book, best_price, _ = max(item["quotes"], key=lambda q: number(q[1], 0))
            raw, team = item["raw"], item["raw"].get("team") or item["raw"].get("team_name") or "Por confirmar"
            if preview_player.get("team"):
                team = preview_player["team"]
            opponent = away if norm(team) == norm(home) else home if norm(team) == norm(away) else f"{away} / {home}"
            score = 100 * probability + min(8, (stat.get("pa") or 0) / 75) + max(-4, min(8, (probability - market_p) * 100))
            if item["kind"] != "Strikeouts" and preview_player.get("lineup_confirmed") and preview_player.get("on_bench"):
                continue
            if item["kind"] != "Strikeouts" and preview_player and not preview_player.get("lineup_confirmed"):
                score -= 4
            if item["kind"] == "Strikeouts" and (
                item["line"] < 2.5
                or (stat.get("pa") or 0) < 30
                or preview.get("probable_ids") and str(stat.get("player_id")) not in preview["probable_ids"]
            ):
                continue
            if probability >= MIN_PROP_PROBABILITY and score >= MIN_PROP_SCORE:
                candidates.append({"player": stat["name"], "player_id": stat.get("player_id"), "team": team, "opponent": opponent, "home": home, "away": away, "market": item["kind"], "side": "Over", "line": item["line"], "probability": probability, "score": min(100.0, score), "price": best_price, "bookmaker": best_book, "statcast_season": savant["season"], "game_pk": preview.get("game_pk"), "lineup_confirmed": preview_player.get("lineup_confirmed")})
    teams = _current_teams(row.get("player_id") for row in candidates)
    for row in candidates:
        official_team = teams.get(str(row.get("player_id")))
        if official_team:
            row["team"] = official_team
            if norm(official_team) == norm(row["home"]):
                row["opponent"] = row["away"]
            elif norm(official_team) == norm(row["away"]):
                row["opponent"] = row["home"]
    unique = {}
    for row in sorted(candidates, key=lambda x: x["score"], reverse=True):
        unique.setdefault((norm(row["player"]), row["market"]), row)
    return list(unique.values())[:10]
