import asyncio
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from valueedge_analysis import analyze_mlb_props, display_odds, implied_probability
from api_sports_analysis import (
    agreement_text,
    confidence_adjustment,
    enrich_events,
    source_text,
)
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# =========================================================
# VALUEEDGEBOT
# MLB + FUTBOL + NBA
# Odds-API.io
# DraftKings + FanDuel
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://api.odds-api.io/v3"
NY_TZ = ZoneInfo("America/New_York")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "valueedge_state.json")

AUTO_HOUR = 8
AUTO_MINUTE = 0

AUTO_TOP = 5
MANUAL_MIN = 7
MANUAL_MAX = 10
MIN_TWO_WAY_PROBABILITY = 0.54
MIN_THREE_WAY_PROBABILITY = 0.42
MIN_LEAD_OVER_SECOND = 0.035

BOOKMAKERS = "DraftKings,FanDuel"


# =========================================================
# LIGAS DE FUTBOL
# =========================================================

SOCCER_LEAGUES = {
    "england-premier-league": "Premier League",
    "spain-laliga": "La Liga",
    "italy-serie-a": "Serie A",
    "germany-bundesliga": "Bundesliga",
    "france-ligue-1": "Ligue 1",
    "usa-mls": "MLS",
    "international-clubs-uefa-champions-league-playoff-round":
        "Champions League",
    "international-clubs-uefa-europa-league-playoff-round":
        "Europa League",
}


if not BOT_TOKEN:
    raise ValueError("Falta BOT_TOKEN en .env")

if not ODDS_API_KEY:
    raise ValueError("Falta ODDS_API_KEY en .env")


# =========================================================
# ESTADO
# =========================================================

def load_state():

    default_subscribers = []
    if TELEGRAM_CHAT_ID:
        try:
            default_subscribers = [int(TELEGRAM_CHAT_ID)]
        except ValueError:
            print("TELEGRAM_CHAT_ID inválido")

    default = {
        "subscribers": default_subscribers,
        "picks": {},
        "last_auto": None
    }

    if not os.path.exists(STATE_FILE):
        return default

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            state = json.load(f)

        state.setdefault(
            "subscribers",
            default_subscribers
        )

        for chat_id in default_subscribers:
            if chat_id not in state["subscribers"]:
                state["subscribers"].append(chat_id)

        state.setdefault(
            "picks",
            {}
        )

        state.setdefault(
            "last_auto",
            None
        )

        return state

    except Exception:

        return default


STATE = load_state()


def save_state():

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            STATE,
            f,
            ensure_ascii=False,
            indent=2
        )


# =========================================================
# FECHA / HORA
# =========================================================

def now_ny():

    return datetime.now(NY_TZ)


def local_datetime(iso):

    try:

        dt = datetime.fromisoformat(
            iso.replace(
                "Z",
                "+00:00"
            )
        )

        return dt.astimezone(
            NY_TZ
        )

    except Exception:

        return None


def valid_event_date(event):

    iso = event.get(
        "date",
        event.get(
            "commence_time",
            ""
        )
    )

    dt = local_datetime(iso)

    if not dt:
        return False

    today = now_ny().date()

    tomorrow = (
        today +
        timedelta(days=1)
    )

    return dt.date() in (
        today,
        tomorrow
    )


def date_text(iso):

    dt = local_datetime(iso)

    if not dt:
        return "N/A"

    return dt.strftime(
        "%m/%d/%Y"
    )


def time_text(iso):

    dt = local_datetime(iso)

    if not dt:
        return "N/A"

    return dt.strftime(
        "%I:%M %p"
    ).lstrip("0")


# =========================================================
# API
# =========================================================

def api_get(
    path,
    params=None
):

    query = {
        "apiKey": ODDS_API_KEY
    }

    if params:
        query.update(params)

    try:

        response = requests.get(
            f"{BASE_URL}/{path}",
            params=query,
            timeout=30
        )

    except Exception as error:

        print(
            f"API CONNECTION ERROR: {error}"
        )

        return None

    if response.status_code != 200:

        print(
            f"API ERROR {response.status_code}: "
            f"{response.text[:500]}"
        )

        return None

    try:

        return response.json()

    except Exception:

        print(
            "API ERROR: respuesta no JSON"
        )

        return None


# =========================================================
# EVENTOS
# =========================================================

def get_events(
    sport,
    league=None,
    limit=100
):

    params = {
        "sport": sport,
        "status": "pending",
        "limit": limit,

    }

    if league:
        params["league"] = league

    data = api_get(
        "events",
        params
    )

    if not data:
        return []

    return [
        event
        for event in data
        if valid_event_date(event)
    ]


def get_soccer_events():

    result = []

    for league_slug, league_name in SOCCER_LEAGUES.items():

        events = get_events(
           "football",
           league_slug,
            100
        )

        for event in events:

            event["_league"] = (
                league_name
            )

            event["_sport_key"] = (
                "football"
            )

        result.extend(events)

    return result


def get_mlb_events():

    events = get_events(
        "baseball",
        "usa-mlb",
        100
    )

    for event in events:

        event["_league"] = "MLB"
        event["_sport_key"] = (
            "baseball"
        )

    return events


def get_nba_events():

    events = get_events(
        "basketball",
        "usa-nba",
        100
    )

    result = []

    for event in events:

        event["_league"] = "NBA"
        event["_sport_key"] = (
            "basketball"
        )

        result.append(event)

    return result

# =========================================================
# ODDS
# =========================================================

def get_odds_for_events(events):

    if not events:
        return []

    result = []

    # Consultamos hasta 10 eventos por llamada.
    # Odds-API.io permite eventIds separados por comas.
    for i in range(0, len(events), 10):

        batch = events[i:i + 10]

        ids = ",".join(
            str(event["id"])
            for event in batch
            if event.get("id") is not None
        )

        if not ids:
            continue

        data = api_get(
            "odds/multi",
            {
                "eventIds": ids,
                "bookmakers": BOOKMAKERS
            }
        )

        if not data:
            continue

        if isinstance(data, list):
            result.extend(data)

        elif isinstance(data, dict):
            result.append(data)

    return result

def attach_odds(events):

    odds_data = get_odds_for_events(
        events
    )

    odds_map = {
        item["id"]: item
        for item in odds_data
        if item.get("id") is not None
    }

    result = []

    for event in events:

        event_id = event.get(
            "id"
        )

        odds = odds_map.get(
            event_id
        )

        if not odds:
            continue

        event["_odds"] = odds

        result.append(event)

    return result


# =========================================================
# PROBABILIDAD
# =========================================================

def american_probability(price):
    return implied_probability(price)


def american(price):
    return display_odds(price)


def bar(probability):

    width = 12

    filled = round(
        probability * width
    )

    filled = max(
        0,
        min(
            width,
            filled
        )
    )

    return (
        "█" * filled +
        "░" * (
            width - filled
        )
    )


# =========================================================
# NORMALIZAR EVENTO
# =========================================================

def event_home(event):

    return event.get(
        "home",
        event.get(
            "home_team",
            "N/A"
        )
    )


def event_away(event):

    return event.get(
        "away",
        event.get(
            "away_team",
            "N/A"
        )
    )


def event_date(event):

    return event.get(
        "date",
        event.get(
            "commence_time",
            ""
        )
    )


# =========================================================
# MONEYLINE
# =========================================================

def get_h2h(event):

    odds = event.get(
        "_odds",
        {}
    )

    bookmakers = odds.get(
        "bookmakers",
        {}
    )

    all_options = {}

    for bookmaker_name, markets in bookmakers.items():

        if not isinstance(
            markets,
            list
        ):
            continue

        for market in markets:

            if market.get(
                "name"
            ) == "ML":

                for item in market.get(
                    "odds",
                    []
                ):

                    for name in (
                        "home",
                        "draw",
                        "away"
                    ):

                        if name not in item:
                            continue

                        price = item.get(
                            name
                        )

                        if price is None:
                            continue

                        if name == "home":
                            display_name = (
                                event_home(event)
                            )

                        elif name == "away":
                            display_name = (
                                event_away(event)
                            )

                        else:
                            display_name = "Draw"

                        probability = (
                            american_probability(
                                price
                            )
                        )

                        if (
                            display_name
                            not in all_options
                            or probability >
                            all_options[
                                display_name
                            ]["raw"]
                        ):

                            all_options[
                                display_name
                            ] = {
                                "price": price,
                                "raw": probability,
                                "bookmaker":
                                    bookmaker_name
                            }

    if not all_options:
        return None

    total = sum(
        item["raw"]
        for item in
        all_options.values()
    )

    if total <= 0:
        return None

    normalized = {}

    for name, item in all_options.items():

        normalized[name] = {
            **item,
            "probability":
                item["raw"] / total
        }

    best_name = max(
        normalized,
        key=lambda name:
            normalized[name][
                "probability"
            ]
    )

    best = normalized[
        best_name
    ]

    return {
        "outcomes": normalized,
        "best_name": best_name,
        "best_probability":
            best["probability"],
        "best_price":
            best["price"],
        "bookmaker":
            best["bookmaker"]
    }


# =========================================================
# SPREAD / TOTAL
# =========================================================

def get_market(
    event,
    market_name
):

    odds = event.get(
        "_odds",
        {}
    )

    bookmakers = odds.get(
        "bookmakers",
        {}
    )

    result = []

    for bookmaker_name, markets in bookmakers.items():

        if not isinstance(
            markets,
            list
        ):
            continue

        for market in markets:

            if market.get(
                "name"
            ) != market_name:

                continue

            for item in market.get(
                "odds",
                []
            ):

                result.append(
                    {
                        **item,
                        "bookmaker":
                            bookmaker_name
                    }
                )

    return result


def get_market_fuzzy(event, terms):

    odds = event.get("_odds", {})
    bookmakers = odds.get("bookmakers", {})
    result = []

    for bookmaker_name, markets in bookmakers.items():

        if not isinstance(markets, list):
            continue

        for market in markets:

            market_name = str(market.get("name", "")).lower()

            if not any(term in market_name for term in terms):
                continue

            for item in market.get("odds", []):
                result.append({**item, "bookmaker": bookmaker_name, "market_name": market.get("name", "")})

    return result


def soccer_specials(event):

    lines = []
    # Solo partido completo: excluye Totals HT/2H y BTTS HT/2H.
    totals = get_market(event, "Totals")
    btts = get_market(event, "Both Teams To Score")

    total_choices = []
    for item in totals:
        point = item.get("hdp", item.get("point", item.get("line", "")))
        over_price = item.get("over_price", item.get("over"))
        under_price = item.get("under_price", item.get("under"))
        if over_price is not None and under_price is not None:
            over_p, under_p = american_probability(over_price), american_probability(under_price)
            total = over_p + under_p
            if total > 0:
                total_choices.extend([
                    (over_p / total, f"Over {point}", over_price, item["bookmaker"]),
                    (under_p / total, f"Under {point}", under_price, item["bookmaker"]),
                ])

    if total_choices:
        probability_value, choice, price, book = max(total_choices, key=lambda row: row[0])
        lines.extend([
            "⚽ OVER / UNDER",
            f"🎯 {choice}",
            f"📊 Probabilidad: {probability_value:.1%}",
            f"💵 {american(price)} · {book}",
            ""
        ])

    btts_choices = []
    for item in btts:
        yes_price = item.get("yes", item.get("yes_price"))
        no_price = item.get("no", item.get("no_price"))
        if yes_price is not None and no_price is not None:
            yes_p, no_p = american_probability(yes_price), american_probability(no_price)
            total = yes_p + no_p
            if total > 0:
                btts_choices.extend([
                    (yes_p / total, "Sí", yes_price, item["bookmaker"]),
                    (no_p / total, "No", no_price, item["bookmaker"]),
                ])

    if btts_choices:
        probability_value, choice, price, book = max(btts_choices, key=lambda row: row[0])
        lines.extend([
            "🥅 AMBOS MARCAN",
            f"🎯 {choice}",
            f"📊 Probabilidad: {probability_value:.1%}",
            f"💵 {american(price)} · {book}",
            ""
        ])

    return lines


# =========================================================
# PROPS NBA
# =========================================================

def get_nba_props(event):

    odds = event.get(
        "_odds",
        {}
    )

    bookmakers = odds.get(
        "bookmakers",
        {}
    )

    props = []

    for bookmaker_name, markets in bookmakers.items():

        if not isinstance(
            markets,
            list
        ):
            continue

        for market in markets:

            name = str(
                market.get(
                    "name",
                    ""
                )
            ).lower()

            # Intentamos identificar props
            # de jugadores.
            if "player" not in name:

                continue

            for item in market.get(
                "odds",
                []
            ):

                player = (
                    item.get("player")
                    or item.get("label")
                    or item.get("description")
                )

                side = (
                    item.get("side")
                    or item.get("name")
                    or ""
                )

                point = (
                    item.get("point")
                    or item.get("hdp")
                )

                price = item.get(
                    "price"
                )

                if not player or price is None:
                    continue

                probability = (
                    american_probability(
                        price
                    )
                )

                props.append(
                    {
                        "market":
                            market.get(
                                "name",
                                "Player Prop"
                            ),
                        "player":
                            player,
                        "side":
                            side,
                        "point":
                            point,
                        "price":
                            price,
                        "probability":
                            probability,
                        "bookmaker":
                            bookmaker_name
                    }
                )

    return props


# =========================================================
# FORMATO
# =========================================================

def header(
    emoji,
    title
):

    return (
        "╔══════════════════════════╗\n"
        f"║ {emoji} {title:<20}║\n"
        "╚══════════════════════════╝"
    )


def format_game(
    event,
    emoji,
    title,
    include_props=False
):

    home = event_home(
        event
    )

    away = event_away(
        event
    )

    h2h = get_h2h(
        event
    )

    spread = get_market(
        event,
        "Spread"
    )

    total = get_market(
        event,
        "Totals"
    )

    odds = event.get(
        "_odds",
        {}
    )

    urls = odds.get(
        "urls",
        {}
    )

    lines = [
        header(
            emoji,
            title
        ),
        "",
        f"🏟️ {home}",
        "        VS",
        f"🏟️ {away}",
        "",
        f"📅 {date_text(event_date(event))}",
        f"🕐 {time_text(event_date(event))}",
        ""
    ]

    # =====================================================
    # MONEYLINE
    # =====================================================

    if h2h:

        lines.extend([
            "💰 MONEYLINE",
            ""
        ])

        for name, data in h2h[
            "outcomes"
        ].items():

            lines.append(
                f"{name:<22}"
                f"{american(data['price'])}"
            )

        lines.extend([
            "",
            "📊 PROBABILIDAD"
        ])

        for name, data in h2h[
            "outcomes"
        ].items():

            probability = data[
                "probability"
            ]

            lines.append(
                f"{name[:18]:<18} "
                f"{bar(probability)} "
                f"{probability:.1%}"
            )

        probability = h2h[
            "best_probability"
        ]

        if probability >= 0.70:

            risk = "🟢 BAJO"

        elif probability >= 0.60:

            risk = "🟡 MEDIO"

        else:

            risk = "🔴 ALTO"

        lines.extend([
            "",
            "⚠️ RIESGO",
            risk,
            "",
            "⭐ MEJOR OPCIÓN",
            f"🔥 {h2h['best_name']} ML",
            f"💵 Cuota: "
            f"{american(h2h['best_price'])}",
            f"📚 {h2h['bookmaker']}"
        ])

    # =====================================================
    # SPREAD
    # =====================================================

    if spread:

        lines.extend([
            "",
            "📈 SPREAD"
        ])

        for item in spread[:6]:

            lines.append(
                f"{item.get('home') or item.get('name', '')} "
                f"{item.get('hdp', '')} "
                f"{american(item.get('home_price', item.get('price', 0)))}"
            )

    # =====================================================
    # TOTAL
    # =====================================================

    if total and event.get("_sport_key") != "football":

        lines.extend([
            "",
            "🎯 TOTAL"
        ])

        for item in total[:6]:

            lines.append(
                f"{item.get('over', 'Over')} "
                f"{item.get('hdp', item.get('point', ''))}"
            )

    # Fútbol: Over/Under siempre que haya una línea, y BTTS solo si la API
    # realmente lo entrega. El análisis nunca depende de BTTS.
    if event.get("_sport_key") == "football":
        special_lines = soccer_specials(event)
        if special_lines:
            lines.extend([""] + special_lines)

    # =====================================================
    # NBA PROPS
    # =====================================================

    if include_props:

        props = get_nba_props(
            event
        )

        if props:

            # Los mejores 10 por probabilidad.
            props.sort(
                key=lambda x:
                    x["probability"],
                reverse=True
            )

            lines.extend([
                "",
                "⭐ MEJORES PLAYER PROPS",
                ""
            ])

            for prop in props[:10]:

                lines.extend([
                    f"👤 {prop['player']}",
                    f"🏀 {prop['market']}",
                    f"➡️ {prop['side']} "
                    f"{prop['point']}",
                    f"📊 Probabilidad: "
                    f"{prop['probability']:.0%}",
                    f"💵 Cuota: "
                    f"{american(prop['price'])}",
                    ""
                ])

    # =====================================================
    # ENLACES
    # =====================================================

    if urls:

        lines.extend([
            "",
            "🔗 APUESTAS"
        ])

        if urls.get(
            "DraftKings"
        ):

            lines.append(
                "🎯 DraftKings: "
                + urls[
                    "DraftKings"
                ]
            )

        if urls.get(
            "FanDuel"
        ):

            lines.append(
                "🎯 FanDuel: "
                + urls[
                    "FanDuel"
                ]
            )

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "📊 ValueEdge • Market Analysis"
    ])

    return "\n".join(
        lines
    )


# =========================================================
# ORDENAR
# =========================================================

def probability(event):

    h2h = get_h2h(
        event
    )

    if not h2h:
        return 0

    adjusted = h2h["best_probability"] + confidence_adjustment(
        event,
        h2h["best_name"]
    )
    return max(0.01, min(0.99, adjusted))


def format_pick_card(event, emoji, title, include_props=False):

    h2h = get_h2h(event)
    if not h2h:
        return None

    selected = h2h["best_name"]
    estimated = probability(event)

    if estimated >= 0.72:
        confidence = "🟢 ALTA"
    elif estimated >= 0.60:
        confidence = "🟡 MEDIA"
    else:
        confidence = "🟠 MODERADA"

    lines = [
        f"{emoji} {title}",
        f"{event_away(event)} @ {event_home(event)}",
        f"📅 {date_text(event_date(event))} · {time_text(event_date(event))}",
        "",
        f"🎯 Pick: {selected} ML",
        f"💵 Cuota: {american(h2h['best_price'])} · {h2h['bookmaker']}",
        f"📊 Probabilidad estimada: {estimated:.1%}",
        f"🛡 Confianza: {confidence}",
        f"🔎 {agreement_text(event, selected)}",
        f"📚 Fuentes: {source_text(event)}",
        f"🧪 {performance_text()}",
    ]

    if event.get("_sport_key") == "football":
        specials = soccer_specials(event)
        if specials:
            lines.extend(["", *specials])

    lines.extend(["", "ValueEdge · análisis, no garantía"])
    return "\n".join(lines)


def performance_text():

    settled = [
        pick for pick in STATE.get("picks", {}).values()
        if pick.get("settled") and isinstance(pick.get("won"), bool)
    ]
    if len(settled) < 10:
        return f"Historial en calibración ({len(settled)}/10 resultados)"
    won = sum(1 for pick in settled if pick.get("won"))
    return f"Acierto histórico verificado: {won / len(settled):.1%} ({len(settled)} picks)"


def sort_best(events):

    valid = []

    for event in events:

        p = probability(
            event
        )

        h2h = get_h2h(event)
        outcomes = sorted(
            (item["probability"] for item in h2h["outcomes"].values()),
            reverse=True
        ) if h2h else []
        minimum = MIN_THREE_WAY_PROBABILITY if len(outcomes) >= 3 else MIN_TWO_WAY_PROBABILITY
        lead = outcomes[0] - outcomes[1] if len(outcomes) >= 2 else 0

        # Un favorito apenas por encima del segundo no es una ventaja util.
        if p >= minimum and lead >= MIN_LEAD_OVER_SECOND:

            valid.append(
                (
                    p,
                    event
                )
            )

    valid.sort(
        key=lambda x:
            x[0],
        reverse=True
    )

    return [
        event
        for _, event in valid
    ]


# =========================================================
# GUARDAR PICK
# =========================================================

def save_pick(
    event,
    chat_id
):

    h2h = get_h2h(
        event
    )

    if not h2h:
        return

    event_id = str(
        event["id"]
    )

    pick = STATE[
        "picks"
    ].get(
        event_id
    )

    if not pick:

        pick = {
            "id":
                event["id"],

            "sport_key":
                event.get(
                    "_sport_key",
                    ""
                ),

            "home_team":
                event_home(event),

            "away_team":
                event_away(event),

            "outcome":
                h2h[
                    "best_name"
                ],

            "price":
                h2h[
                    "best_price"
                ],

            "probability":
                probability(event),

            "model_version":
                "valueedge-2-api-sports",

            "date":
                event_date(event),

            "sent_to":
                [],

            "settled":
                False,

            "won":
                None,

            "score":
                None
        }

        STATE[
            "picks"
        ][event_id] = pick

    if chat_id not in pick[
        "sent_to"
    ]:

        pick[
            "sent_to"
        ].append(
            chat_id
        )

    save_state()


# =========================================================
# MENSAJES
# =========================================================

def split_message(text):

    if len(text) <= 3900:

        return [
            text
        ]

    chunks = []
    current = ""

    for line in text.splitlines(
        keepends=True
    ):

        if (
            len(current)
            +
            len(line)
            > 3900
        ):

            if current:
                chunks.append(
                    current
                )

            current = line

        else:

            current += line

    if current:

        chunks.append(
            current
        )

    return chunks


async def send_text(
    bot,
    chat_id,
    text
):

    for chunk in split_message(
        text
    ):

        await bot.send_message(
            chat_id=chat_id,
            text=chunk
        )


# =========================================================
# MANUAL MLB
# =========================================================

async def manual_mlb(
    update,
    context
):

    await update.message.reply_text(
        "🔎 Buscando MLB de HOY + MAÑANA..."
    )

    events = await asyncio.to_thread(
        get_mlb_events
    )

    events = await asyncio.to_thread(
        attach_odds,
        events
    )

    events = await asyncio.to_thread(enrich_events, events, "baseball")

    if not events:

        await update.message.reply_text(
            "❌ No hay partidos MLB "
            "disponibles de HOY + MAÑANA."
        )

        return

    events.sort(
        key=lambda event:
            event_date(event)
    )

    selected = events[:max(MANUAL_MIN, min(MANUAL_MAX, len(events)))]

    await update.message.reply_text(
        f"⚾ MLB\n"
        f"📅 HOY + MAÑANA\n"
        f"📊 {len(selected)} partidos"
    )

    chat_id = (
        update.effective_chat.id
    )

    for event in selected:

        save_pick(
            event,
            chat_id
        )

        text = await asyncio.to_thread(
            format_pick_card,
            event,
            "⚾",
            "MLB",
            False
        )

        if not text:
            continue

        await send_text(
            context.bot,
            chat_id,
            text
        )


# =========================================================
# MANUAL FUTBOL
# =========================================================

async def manual_soccer(
    update,
    context
):

    await update.message.reply_text(
        "🔎 Buscando FÚTBOL de HOY + MAÑANA..."
    )

    events = await asyncio.to_thread(
        get_soccer_events
    )

    events = await asyncio.to_thread(
        attach_odds,
        events
    )

    events = await asyncio.to_thread(enrich_events, events, "football")

    if not events:

        await update.message.reply_text(
            "❌ No hay partidos de fútbol "
            "disponibles de HOY + MAÑANA."
        )

        return

    events.sort(
        key=lambda event:
            event_date(event)
    )

    selected = events[:max(MANUAL_MIN, min(MANUAL_MAX, len(events)))]

    await update.message.reply_text(
        f"⚽ FÚTBOL\n"
        f"📅 HOY + MAÑANA\n"
        f"📊 {len(selected)} partidos"
    )

    chat_id = (
        update.effective_chat.id
    )

    for event in selected:

        save_pick(
            event,
            chat_id
        )

        title = event.get(
            "_league",
            "Fútbol"
        )

        text = await asyncio.to_thread(
            format_pick_card,
            event,
            "⚽",
            title,
            False
        )

        if not text:
            continue

        await send_text(
            context.bot,
            chat_id,
            text
        )


# =========================================================
# MANUAL NBA
# =========================================================

async def manual_nba(
    update,
    context
):

    await update.message.reply_text(
        "🔎 Buscando NBA de HOY + MAÑANA..."
    )

    events = await asyncio.to_thread(
        get_nba_events
    )

    events = await asyncio.to_thread(
        attach_odds,
        events
    )

    events = await asyncio.to_thread(enrich_events, events, "basketball")

    if not events:

        await update.message.reply_text(
            "❌ No hay partidos NBA "
            "disponibles de HOY + MAÑANA."
        )

        return

    events.sort(
        key=lambda event:
            event_date(event)
    )

    selected = events[:max(MANUAL_MIN, min(MANUAL_MAX, len(events)))]

    await update.message.reply_text(
        f"🏀 NBA\n"
        f"📅 HOY + MAÑANA\n"
        f"📊 {len(selected)} partidos"
    )

    chat_id = (
        update.effective_chat.id
    )

    for event in selected:

        save_pick(
            event,
            chat_id
        )

        text = await asyncio.to_thread(
            format_pick_card,
            event,
            "🏀",
            "NBA",
            True
        )

        if not text:
            continue

        await send_text(
            context.bot,
            chat_id,
            text
        )


# =========================================================
# PARLAY MLB - PROPS DE JUGADORES
# =========================================================

def format_parlay(props):

    lines = [
        "⚾ TOP PROPS MLB PARA /PARLAY",
        "",
        "Statcast + línea real de DraftKings/FanDuel",
        "Solo aparecen selecciones que superan los filtros.",
        ""
    ]

    for position, prop in enumerate(props, 1):

        line = int(prop["line"]) if float(prop["line"]).is_integer() else prop["line"]

        lines.extend([
            f"{position}. 👤 {prop['player']}",
            f"   🧢 {prop['team']} vs {prop['opponent']}",
            f"   🎯 {prop['side']} {line} {prop['market']}",
            f"   📊 Probabilidad estimada: {prop['probability']:.1%}",
            f"   ⭐ Score ValueEdge: {prop['score']:.1f}/100",
            f"   💵 {american(prop['price'])} · {prop['bookmaker']}",
            "   ⏳ Alineación por confirmar" if prop.get("lineup_confirmed") is False else "   ✅ Alineación confirmada",
            ""
        ])

    lines.extend([
        "No es necesario combinar todas las selecciones.",
        "ValueEdge no rellena la lista con props débiles."
    ])

    return "\n".join(lines)


async def parlay(update, context):

    await update.message.reply_text(
        "🔎 Analizando props MLB con Statcast y líneas disponibles..."
    )

    events = await asyncio.to_thread(get_mlb_events)
    events = await asyncio.to_thread(attach_odds, events)

    if not events:
        await update.message.reply_text(
            "❌ No hay juegos MLB con líneas disponibles para hoy o mañana."
        )
        return

    props = await asyncio.to_thread(analyze_mlb_props, events)

    if not props:
        await update.message.reply_text(
            "⚠️ Hoy no encontré props MLB que superen los filtros de calidad.\n\n"
            "No voy a rellenar el parlay con picks débiles. Revisa de nuevo cuando "
            "las alineaciones y líneas de jugadores estén publicadas."
        )
        return

    await send_text(context.bot, update.effective_chat.id, format_parlay(props))


# =========================================================
# BEST
# =========================================================

async def best(
    update,
    context
):

    await update.message.reply_text(
        "🔥 Buscando las mejores oportunidades..."
    )

    mlb = await asyncio.to_thread(
        get_mlb_events
    )

    soccer = await asyncio.to_thread(
        get_soccer_events
    )

    nba = await asyncio.to_thread(
        get_nba_events
    )

    all_events = (
        mlb +
        soccer +
        nba
    )

    all_events = await asyncio.to_thread(
        attach_odds,
        all_events
    )

    mlb = [
        e for e in all_events
        if e.get("_sport_key")
        == "baseball"
    ]

    soccer = [
        e for e in all_events
        if e.get("_sport_key")
        == "football"
    ]

    nba = [
        e for e in all_events
        if e.get("_sport_key")
        == "basketball"
    ]

    mlb = await asyncio.to_thread(enrich_events, mlb, "baseball")
    soccer = await asyncio.to_thread(enrich_events, soccer, "football")
    nba = await asyncio.to_thread(enrich_events, nba, "basketball")

    mlb = sort_best(
        mlb
    )[:AUTO_TOP]

    soccer = sort_best(
        soccer
    )[:AUTO_TOP]

    nba = sort_best(
        nba
    )[:AUTO_TOP]

    groups = []

    chat_id = (
        update.effective_chat.id
    )

    for event in mlb:

        save_pick(
            event,
            chat_id
        )

        groups.append(
            await asyncio.to_thread(
                format_pick_card,
                event,
                "⚾",
                "MLB",
                False
            )
        )

    for event in soccer:

        save_pick(
            event,
            chat_id
        )

        groups.append(
            await asyncio.to_thread(
                format_pick_card,
                event,
                "⚽",
                event.get(
                    "_league",
                    "Fútbol"
                ),
                False
            )
        )

    for event in nba:

        save_pick(
            event,
            chat_id
        )

        groups.append(
            await asyncio.to_thread(
                format_pick_card,
                event,
                "🏀",
                "NBA",
                True
            )
        )

    if not groups:

        await update.message.reply_text(
            "❌ No encontré partidos "
            "disponibles."
        )

        return

    await send_text(
        context.bot,
        chat_id,
        "\n\n".join(
            groups
        )
    )


# =========================================================
# RESULTADOS
# =========================================================

def check_score(
    event_id
):

    data = api_get(
        f"events/{event_id}"
    )

    if not data:
        return None

    status = str(
        data.get(
            "status",
            ""
        )
    ).lower()

    # Odds-API.io utiliza estados como
    # pending, live y settled.
    if status not in {
        "settled",
        "finished",
        "completed"
    }:

        return None

    scores = data.get(
        "scores"
    )

    if not scores:
        return None

    home = scores.get(
        "home"
    )

    away = scores.get(
        "away"
    )

    if home is None or away is None:

        return None

    return {
        "home":
            home,

        "away":
            away,

        "status":
            status
    }


def settle_result(
    pick,
    scores
):

    try:

        home = float(
            scores["home"]
        )

        away = float(
            scores["away"]
        )

    except Exception:

        return None

    if home > away:

        winner = pick[
            "home_team"
        ]

    elif away > home:

        winner = pick[
            "away_team"
        ]

    else:

        winner = "Draw"

    won = (
        pick["outcome"]
        ==
        winner
    )

    return (
        won,
        f"{int(home)} - {int(away)}"
    )


# =========================================================
# RESULTADOS
# =========================================================

LAST_RESULTS_CHECK = None
RESULTS_CHECK_MINUTES = 15
MAX_RESULTS_PER_CHECK = 5


async def check_results(
    application
):

    global LAST_RESULTS_CHECK

    current_time = now_ny()

    # Revisar resultados como máximo cada 10 minutos.
    if LAST_RESULTS_CHECK is not None:

        elapsed = (
            current_time -
            LAST_RESULTS_CHECK
        ).total_seconds()

        if elapsed < 600:
            return

    LAST_RESULTS_CHECK = current_time

    pending = [
        pick
        for pick in STATE[
            "picks"
        ].values()
        if not pick.get("settled")
    ]

    if not pending:
        return

    # Solo revisar hasta 3 partidos por ciclo.
    pending = pending[:3]

    for pick in pending:

        event_id = str(
            pick.get("id", "")
        )

        # Odds-API.io usa IDs numéricos.
        # Evitamos consultar IDs antiguos/inválidos.
        if not event_id.isdigit():
            print(
                f"Saltando ID inválido: {event_id}"
            )

            pick["settled"] = True
            pick["won"] = None
            pick["score"] = "ID inválido"

            save_state()
            continue

        result = await asyncio.to_thread(
            check_score,
            int(event_id)
        )

        if not result:
            continue

        settled = settle_result(
            pick,
            result
        )

        if not settled:
            continue

        won, score = settled

        status = (
            "✅ GANADA"
            if won
            else
            "❌ PERDIDA"
        )

        message = (
            f"{status}\n\n"
            f"🏟️ "
            f"{pick['home_team']}\n"
            f"        VS\n"
            f"🏟️ "
            f"{pick['away_team']}\n\n"
            f"🎯 Tu selección: "
            f"{pick['outcome']}\n"
            f"📊 Resultado: "
            f"{score}"
        )

        for chat_id in pick.get(
            "sent_to",
            []
        ):

            try:

                await application.bot.send_message(
                    chat_id=chat_id,
                    text=message
                )

            except Exception as error:

                print(
                    f"Error resultado: "
                    f"{error}"
                )

        pick["settled"] = True
        pick["won"] = won
        pick["score"] = score

        save_state()

# =========================================================
# AUTOMATICO 8 AM
# =========================================================

async def automatic_send(
    application
):

    today = (
        now_ny()
        .date()
        .isoformat()
    )

    if STATE.get(
        "last_auto"
    ) == today:

        return

    if not STATE[
        "subscribers"
    ]:

        return

    print(
        "Preparando recomendaciones automáticas..."
    )

    mlb = await asyncio.to_thread(
        get_mlb_events
    )

    soccer = await asyncio.to_thread(
        get_soccer_events
    )

    nba = await asyncio.to_thread(
        get_nba_events
    )

    all_events = (
        mlb +
        soccer +
        nba
    )

    all_events = await asyncio.to_thread(
        attach_odds,
        all_events
    )

    mlb = [
        e for e in all_events
        if e.get("_sport_key")
        == "baseball"
    ]

    soccer = [
        e for e in all_events
        if e.get("_sport_key")
        == "football"
    ]

    nba = [
        e for e in all_events
        if e.get("_sport_key")
        == "basketball"
    ]

    mlb = await asyncio.to_thread(enrich_events, mlb, "baseball")
    soccer = await asyncio.to_thread(enrich_events, soccer, "football")
    nba = await asyncio.to_thread(enrich_events, nba, "basketball")

    mlb = sort_best(
        mlb
    )[:AUTO_TOP]

    soccer = sort_best(
        soccer
    )[:AUTO_TOP]

    nba = sort_best(
        nba
    )[:AUTO_TOP]

    delivered = False

    for chat_id in STATE[
        "subscribers"
    ]:

        parts = []

        if mlb:

            mlb_text = []

            for event in mlb:

                save_pick(
                    event,
                    chat_id
                )

                mlb_text.append(
                    await asyncio.to_thread(
                        format_pick_card,
                        event,
                        "⚾",
                        "MLB",
                        False
                    )
                )

            parts.append(
                "🔥 TOP 5 MLB\n\n"
                +
                "\n\n".join(
                    mlb_text
                )
            )

        if soccer:

            soccer_text = []

            for event in soccer:

                save_pick(
                    event,
                    chat_id
                )

                soccer_text.append(
                    await asyncio.to_thread(
                        format_pick_card,
                        event,
                        "⚽",
                        event.get(
                            "_league",
                            "Fútbol"
                        ),
                        False
                    )
                )

            parts.append(
                "🔥 TOP 5 FÚTBOL\n\n"
                +
                "\n\n".join(
                    soccer_text
                )
            )

        if nba:

            nba_text = []

            for event in nba:

                save_pick(
                    event,
                    chat_id
                )

                nba_text.append(
                    await asyncio.to_thread(
                        format_pick_card,
                        event,
                        "🏀",
                        "NBA",
                        True
                    )
                )

            parts.append(
                "🔥 TOP 5 NBA\n\n"
                +
                "\n\n".join(
                    nba_text
                )
            )

        if parts:
            try:

                # Un mensaje separado por deporte: más fácil de leer y guardar.
                for part in parts:
                    await send_text(application.bot, chat_id, part)

                delivered = True

            except Exception as error:

                print(
                    f"Error automático: "
                    f"{error}"
                )

    # Si la API no devolvió picks o falló el envío, el scheduler puede
    # reintentarlo en el próximo ciclo en vez de perder todo el día.
    if delivered:
        STATE["last_auto"] = today
        save_state()


# =========================================================
# SCHEDULER
# =========================================================

async def scheduler(
    application
):

    print(
        "Programación automática: "
        "8:00 AM America/New_York"
    )

    while True:

        try:

            await check_results(
                application
            )

            current = now_ny()

            if (
                current.hour >
                AUTO_HOUR
                or
                (
                    current.hour ==
                    AUTO_HOUR
                    and
                    current.minute >=
                    AUTO_MINUTE
                )
            ):

                await automatic_send(
                    application
                )

        except Exception as error:

            print(
                f"Error scheduler: "
                f"{error}"
            )

        await asyncio.sleep(
            60
        )


# =========================================================
# START
# =========================================================

async def start(
    update,
    context
):

    chat_id = (
        update.effective_chat.id
    )

    if chat_id not in STATE[
        "subscribers"
    ]:

        STATE[
            "subscribers"
        ].append(
            chat_id
        )

        save_state()

    await update.message.reply_text(
        "🤖 ValueEdgeBot activado.\n\n"
        "🕗 Automático: 8:00 AM NY\n"
        "📅 Partidos: HOY + MAÑANA\n"
        "📚 DraftKings + FanDuel\n\n"
        "⚾ /mlb\n"
        "⚽ /futbol\n"
        "🏀 /nba\n"
        "🧩 /parlay (props MLB)\n"
        "🔥 /best"
    )


# =========================================================
# POST INIT
# =========================================================

async def post_init(
    application
):

    application.bot_data[
        "scheduler"
    ] = asyncio.create_task(
        scheduler(
            application
        )
    )

    print(
        "================================"
    )

    print(
        "ValueEdgeBot iniciado"
    )

    print(
        "Odds-API.io"
    )

    print(
        "MLB + Fútbol + NBA"
    )

    print(
        "DraftKings + FanDuel"
    )

    print(
        "Partidos: HOY + MAÑANA"
    )

    print(
        "Automático: 8:00 AM NY"
    )

    print(
        "================================"
    )


# =========================================================
# POST SHUTDOWN
# =========================================================

async def post_shutdown(
    application
):

    task = application.bot_data.get(
        "scheduler"
    )

    if task:

        task.cancel()

        try:

            await task

        except asyncio.CancelledError:

            pass


# =========================================================
# TELEGRAM
# =========================================================

app = (
    Application
    .builder()
    .token(
        BOT_TOKEN
    )
    .post_init(
        post_init
    )
    .post_shutdown(
        post_shutdown
    )
    .build()
)


app.add_handler(
    CommandHandler(
        "start",
        start
    )
)

app.add_handler(
    CommandHandler(
        "best",
        best
    )
)

app.add_handler(
    CommandHandler(
        "mlb",
        manual_mlb
    )
)

app.add_handler(
    CommandHandler(
        "futbol",
        manual_soccer
    )
)

app.add_handler(
    CommandHandler(
        "nba",
        manual_nba
    )
)

app.add_handler(
    CommandHandler(
        "parlay",
        parlay
    )
)


# =========================================================
# INICIAR
# =========================================================

if __name__ == "__main__":

    print(
        "Iniciando ValueEdgeBot..."
    )

    app.run_polling()
