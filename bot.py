import os
import requests
from datetime import datetime
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =========================================================
# CONFIGURACIÓN
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

BASE_URL = "https://api.the-odds-api.com/v4/sports"

if not BOT_TOKEN:
    raise ValueError("Falta BOT_TOKEN en el archivo .env")

if not ODDS_API_KEY:
    raise ValueError("Falta ODDS_API_KEY en el archivo .env")


# =========================================================
# UTILIDADES
# =========================================================

def american_to_probability(odds):
    odds = float(odds)

    if odds > 0:
        return 100 / (odds + 100)

    return abs(odds) / (abs(odds) + 100)


def probability_bar(probability, length=12):
    probability = max(0, min(1, probability))

    filled = round(probability * length)
    empty = length - filled

    return "█" * filled + "░" * empty


def risk_level(probability):
    percentage = probability * 100

    if percentage >= 65:
        return "🟢 BAJO"
    elif percentage >= 55:
        return "🟡 MEDIO"
    elif percentage >= 50:
        return "🟠 ALTO"
    else:
        return "❌ MUY ALTO"


def format_odds(odds):
    odds = int(odds)

    if odds > 0:
        return f"+{odds}"

    return str(odds)


def format_date(commence_time):
    try:
        dt = datetime.fromisoformat(
            commence_time.replace("Z", "+00:00")
        )

        # Convierte a la hora local de Windows
        dt = dt.astimezone()

        date_text = dt.strftime("%m/%d/%Y")
        time_text = dt.strftime("%I:%M %p").lstrip("0")

        return date_text, time_text

    except Exception:
        return "N/A", "N/A"


# =========================================================
# API
# =========================================================

def get_games(sport):
    url = f"{BASE_URL}/{sport}/odds"

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "american",
    }

    try:
        response = requests.get(
            url,
            params=params,
            timeout=20
        )

        if response.status_code != 200:
            print("ERROR API:", response.status_code)
            print(response.text[:500])
            return []

        return response.json()

    except Exception as error:
        print("ERROR:", error)
        return []


# =========================================================
# BOOKMAKER
# =========================================================

def get_best_bookmaker(game):
    bookmakers = game.get("bookmakers", [])

    if not bookmakers:
        return None

    return bookmakers[0]


def get_markets(bookmaker):
    if not bookmaker:
        return {}

    return {
        market["key"]: market
        for market in bookmaker.get("markets", [])
    }


# =========================================================
# PROBABILIDAD MONEYLINE
# =========================================================

def moneyline_analysis(outcomes):
    data = []

    for outcome in outcomes:
        name = outcome["name"]
        price = outcome["price"]

        probability = american_to_probability(price)

        data.append(
            {
                "name": name,
                "price": price,
                "probability": probability,
            }
        )

    total = sum(item["probability"] for item in data)

    if total <= 0:
        return data, None

    for item in data:
        item["normalized"] = item["probability"] / total

    best = max(
        data,
        key=lambda item: item["normalized"]
    )

    return data, best


# =========================================================
# ENCABEZADO
# =========================================================

def header(sport, emoji):
    return (
        "╔══════════════════════════╗\n"
        f"║ {emoji} {sport:<20} ║\n"
        "╚══════════════════════════╝"
    )


# =========================================================
# MLB
# =========================================================

def format_mlb_game(game):

    home = game.get("home_team", "Local")
    away = game.get("away_team", "Visitante")

    date_text, time_text = format_date(
        game.get("commence_time", "")
    )

    bookmaker = get_best_bookmaker(game)

    if not bookmaker:
        return None

    markets = get_markets(bookmaker)

    h2h = markets.get("h2h")
    spreads = markets.get("spreads")
    totals = markets.get("totals")

    lines = []

    lines.append(header("MLB", "⚾"))
    lines.append("")
    lines.append(f"🏟️ {away}")
    lines.append("        VS")
    lines.append(f"🏟️ {home}")
    lines.append("")
    lines.append(f"📅 {date_text}")
    lines.append(f"🕐 {time_text}")
    lines.append(f"📚 {bookmaker.get('title', 'Sportsbook')}")
    lines.append("")

    best = None

    # MONEYLINE
    if h2h and h2h.get("outcomes"):

        data, best = moneyline_analysis(
            h2h["outcomes"]
        )

        lines.append("💰 MONEYLINE")

        for item in data:
            lines.append(
                f"{item['name']}  {format_odds(item['price'])}"
            )

        lines.append("")
        lines.append("📊 PROBABILIDAD")

        for item in data:
            percentage = item["normalized"] * 100

            lines.append(
                f"{item['name'][:18]:18} "
                f"{probability_bar(item['normalized'])} "
                f"{percentage:.1f}%"
            )

        lines.append("")

    # SPREAD
    if spreads and spreads.get("outcomes"):

        lines.append("📈 SPREAD")

        for outcome in spreads["outcomes"]:

            point = outcome.get("point", "")

            lines.append(
                f"{outcome['name']} {point}  "
                f"{format_odds(outcome['price'])}"
            )

        lines.append("")

    # TOTAL
    if totals and totals.get("outcomes"):

        lines.append("🎯 TOTAL")

        for outcome in totals["outcomes"]:

            point = outcome.get("point", "")
            label = outcome["name"].upper()

            lines.append(
                f"{label} {point}  "
                f"{format_odds(outcome['price'])}"
            )

        lines.append("")

    if best:

        lines.append("⚠️ RIESGO")
        lines.append(
            risk_level(best["normalized"])
        )
        lines.append("")

        lines.append("⭐ MEJOR OPCIÓN")
        lines.append(
            f"🔥 {best['name']} ML"
        )

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("📊 ValueEdge • Market Analysis")

    return "\n".join(lines)


# =========================================================
# FÚTBOL
# =========================================================

def format_soccer_game(game):

    home = game.get("home_team", "Local")
    away = game.get("away_team", "Visitante")

    date_text, time_text = format_date(
        game.get("commence_time", "")
    )

    bookmaker = get_best_bookmaker(game)

    if not bookmaker:
        return None

    markets = get_markets(bookmaker)

    h2h = markets.get("h2h")
    totals = markets.get("totals")
    btts = markets.get("btts")

    lines = []

    lines.append(header("FÚTBOL", "⚽"))
    lines.append("")
    lines.append(f"🏟️ {away}")
    lines.append("        VS")
    lines.append(f"🏟️ {home}")
    lines.append("")
    lines.append(f"📅 {date_text}")
    lines.append(f"🕐 {time_text}")
    lines.append(f"📚 {bookmaker.get('title', 'Sportsbook')}")
    lines.append("")

    best = None

    # MONEYLINE
    if h2h and h2h.get("outcomes"):

        data, best = moneyline_analysis(
            h2h["outcomes"]
        )

        lines.append("💰 MONEYLINE")

        for item in data:
            lines.append(
                f"{item['name']}  "
                f"{format_odds(item['price'])}"
            )

        lines.append("")
        lines.append("📊 PROBABILIDAD")

        for item in data:

            percentage = item["normalized"] * 100

            lines.append(
                f"{item['name'][:16]:16} "
                f"{probability_bar(item['normalized'])} "
                f"{percentage:.1f}%"
            )

        lines.append("")

    # BTTS
    if btts and btts.get("outcomes"):

        data, _ = moneyline_analysis(
            btts["outcomes"]
        )

        lines.append("⚽ BTTS")

        for item in data:

            percentage = item["normalized"] * 100

            if item["name"].lower() == "yes":
                icon = "✅"
            else:
                icon = "❌"

            lines.append(
                f"{icon} {item['name']}  "
                f"{format_odds(item['price'])}"
            )

            lines.append(
                f"   {probability_bar(item['normalized'])} "
                f"{percentage:.1f}%"
            )

        lines.append("")

    # OVER / UNDER
    if totals and totals.get("outcomes"):

        data = []

        for outcome in totals["outcomes"]:

            price = outcome["price"]

            data.append(
                {
                    "name": outcome["name"],
                    "point": outcome.get("point", ""),
                    "price": price,
                    "probability": american_to_probability(price),
                }
            )

        total = sum(
            item["probability"]
            for item in data
        )

        lines.append("🎯 OVER / UNDER")

        for item in data:

            normalized = (
                item["probability"] / total
                if total > 0
                else 0
            )

            percentage = normalized * 100

            lines.append(
                f"{item['name'].upper()} "
                f"{item['point']}  "
                f"{format_odds(item['price'])}"
            )

            lines.append(
                f"   {probability_bar(normalized)} "
                f"{percentage:.1f}%"
            )

        lines.append("")

    if best:

        lines.append("⚠️ RIESGO")
        lines.append(
            risk_level(best["normalized"])
        )
        lines.append("")

        lines.append("⭐ MEJOR OPCIÓN")
        lines.append(
            f"🔥 {best['name']} ML"
        )

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("📊 ValueEdge • Market Analysis")

    return "\n".join(lines)


# =========================================================
# NBA
# =========================================================

def format_nba_game(game):

    home = game.get("home_team", "Local")
    away = game.get("away_team", "Visitante")

    date_text, time_text = format_date(
        game.get("commence_time", "")
    )

    bookmaker = get_best_bookmaker(game)

    if not bookmaker:
        return None

    markets = get_markets(bookmaker)

    h2h = markets.get("h2h")
    spreads = markets.get("spreads")
    totals = markets.get("totals")

    lines = []

    lines.append(header("NBA", "🏀"))
    lines.append("")
    lines.append(f"🏟️ {away}")
    lines.append("        VS")
    lines.append(f"🏟️ {home}")
    lines.append("")
    lines.append(f"📅 {date_text}")
    lines.append(f"🕐 {time_text}")
    lines.append(f"📚 {bookmaker.get('title', 'Sportsbook')}")
    lines.append("")

    best = None

    # MONEYLINE
    if h2h and h2h.get("outcomes"):

        data, best = moneyline_analysis(
            h2h["outcomes"]
        )

        lines.append("💰 MONEYLINE")

        for item in data:
            lines.append(
                f"{item['name']}  "
                f"{format_odds(item['price'])}"
            )

        lines.append("")
        lines.append("📊 PROBABILIDAD")

        for item in data:

            percentage = item["normalized"] * 100

            lines.append(
                f"{item['name'][:18]:18} "
                f"{probability_bar(item['normalized'])} "
                f"{percentage:.1f}%"
            )

        lines.append("")

    # SPREAD
    if spreads and spreads.get("outcomes"):

        lines.append("📈 SPREAD")

        for outcome in spreads["outcomes"]:

            point = outcome.get("point", "")

            lines.append(
                f"{outcome['name']} {point}  "
                f"{format_odds(outcome['price'])}"
            )

        lines.append("")

    # TOTAL
    if totals and totals.get("outcomes"):

        lines.append("🎯 TOTAL")

        for outcome in totals["outcomes"]:

            point = outcome.get("point", "")

            lines.append(
                f"{outcome['name'].upper()} {point}  "
                f"{format_odds(outcome['price'])}"
            )

        lines.append("")

    if best:

        lines.append("⚠️ RIESGO")
        lines.append(
            risk_level(best["normalized"])
        )
        lines.append("")

        lines.append("⭐ MEJOR OPCIÓN")
        lines.append(
            f"🔥 {best['name']} ML"
        )

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("📊 ValueEdge • Market Analysis")

    return "\n".join(lines)


# =========================================================
# BOTONES
# =========================================================

def main_keyboard():

    keyboard = [
        [
            InlineKeyboardButton(
                "⚾ MLB",
                callback_data="mlb"
            ),
            InlineKeyboardButton(
                "⚽ FÚTBOL",
                callback_data="futbol"
            ),
        ],
        [
            InlineKeyboardButton(
                "🏀 NBA",
                callback_data="nba"
            ),
        ],
        [
            InlineKeyboardButton(
                "🔄 ACTUALIZAR",
                callback_data="menu"
            ),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# MOSTRAR PARTIDOS
# =========================================================

async def send_sport(
    update,
    context,
    sport,
    formatter,
    loading_text
):

    if update.callback_query:
        await update.callback_query.answer()

        await update.callback_query.message.reply_text(
            loading_text
        )

        send_message = (
            update.callback_query.message.reply_text
        )

    else:

        await update.message.reply_text(
            loading_text
        )

        send_message = update.message.reply_text

    games = get_games(sport)

    if not games:

        await send_message(
            "⚠️ No hay partidos disponibles "
            "en este momento."
        )

        return

    sent = 0

    for game in games:

        message = formatter(game)

        if message:

            await send_message(message)

            sent += 1

    if sent == 0:

        await send_message(
            "⚠️ No pude encontrar mercados "
            "disponibles para estos partidos."
        )


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.application.bot_data["chat_id"] = update.effective_chat.id
    
    text = (
        "🔥 VALUEEDGE BOT 🔥\n\n"
        "📊 ANÁLISIS DEPORTIVO\n\n"
        "Selecciona un deporte:\n\n"
        "⚾ MLB\n"
        "⚽ Fútbol\n"
        "🏀 NBA\n\n"
        "🔄 Puedes actualizar los partidos "
        "cuando quieras."
    )

    await update.message.reply_text(
        text,
        reply_markup=main_keyboard()
    )


# =========================================================
# COMANDOS
# =========================================================

async def mlb(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await send_sport(
        update,
        context,
        "baseball_mlb",
        format_mlb_game,
        "🔎 Buscando partidos MLB..."
    )


async def futbol(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await send_sport(
        update,
        context,
        "soccer_usa_mls",
        format_soccer_game,
        "🔎 Buscando partidos de fútbol..."
    )


async def nba(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await send_sport(
        update,
        context,
        "basketball_nba",
        format_nba_game,
        "🔎 Buscando partidos NBA..."
    )


# =========================================================
# BOTONES
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if query.data == "menu":

        await query.answer()

        await query.message.reply_text(
            "🔥 VALUEEDGE\n\n"
            "Selecciona el deporte:",
            reply_markup=main_keyboard()
        )

        return

    if query.data == "mlb":
        await send_sport(
            update,
            context,
            "baseball_mlb",
            format_mlb_game,
            "🔎 Buscando partidos MLB..."
        )

    elif query.data == "futbol":
        await send_sport(
            update,
            context,
            "soccer_usa_mls",
            format_soccer_game,
            "🔎 Buscando partidos de fútbol..."
        )

    elif query.data == "nba":
        await send_sport(
            update,
            context,
            "basketball_nba",
            format_nba_game,
            "🔎 Buscando partidos NBA..."
        )


# =========================================================
# INICIAR
# =========================================================

app = (
    Application
    .builder()
    .token(BOT_TOKEN)
    .build()
)

app.add_handler(
    CommandHandler("start", start)
)

app.add_handler(
    CommandHandler("mlb", mlb)
)

app.add_handler(
    CommandHandler("futbol", futbol)
)

app.add_handler(
    CommandHandler("nba", nba)
)

app.add_handler(
    CallbackQueryHandler(button_handler)
)

print("🔥 ValueEdge Bot está funcionando...")
# =========================================================
# ACTUALIZACIÓN AUTOMÁTICA DE PARTIDOS
# =========================================================

async def automatic_updates(context):
    chat_id = context.application.bot_data.get("chat_id")

    if not chat_id:
        return

    sports = [
        ("baseball_mlb", format_mlb_game),
        ("soccer_usa_mls", format_soccer_game),
        ("basketball_nba", format_nba_game),
    ]

    if "sent_games" not in context.application.bot_data:
        context.application.bot_data["sent_games"] = set()

    sent_games = context.application.bot_data["sent_games"]

    for sport, formatter in sports:

        games = get_games(sport)

        for game in games:

            game_id = game.get("id")

            if not game_id:
                continue

            # No repetir partidos que ya fueron enviados
            if game_id in sent_games:
                continue

            message = formatter(game)

            if message:

                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=message
                    )

                    sent_games.add(game_id)

                except Exception as error:
                    print("ERROR enviando partido:", error)


# =========================================================
# INICIAR ACTUALIZACIÓN CADA 1 MINUTO
# =========================================================

async def post_init(application):

    application.job_queue.run_repeating(
        automatic_updates,
        interval=60,
        first=5
    )


# =========================================================
# INICIAR BOT
# =========================================================

app.run_polling()
