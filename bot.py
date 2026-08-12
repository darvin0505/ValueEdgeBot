import asyncio
import json
import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
BASE_URL = "https://api.the-odds-api.com/v4/sports"

# Configuracion
RESULT_CHECK_INTERVAL_SECONDS = 600
SCHEDULER_TICK_SECONDS = 30
# Usa la zona horaria configurada en Windows. En este equipo es Nueva York,
# y asi no depende del paquete opcional tzdata.
BOT_TIMEZONE = datetime.now().astimezone().tzinfo
MAX_PICKS_PER_SCAN = 3
FOOTBALL_MIN_PROBABILITY = 0.58
MLB_MIN_PROBABILITY = 0.65
STATE_FILE = "valueedge_state.json"

# Puedes quitar ligas que no te interesen. Todos los soccer_* son futbol.
SPORTS = {
    "soccer_epl": {"name": "Futbol - Premier League", "emoji": "⚽", "minimum": FOOTBALL_MIN_PROBABILITY},
    "soccer_spain_la_liga": {"name": "Futbol - La Liga", "emoji": "⚽", "minimum": FOOTBALL_MIN_PROBABILITY},
    "soccer_italy_serie_a": {"name": "Futbol - Serie A", "emoji": "⚽", "minimum": FOOTBALL_MIN_PROBABILITY},
    "soccer_germany_bundesliga": {"name": "Futbol - Bundesliga", "emoji": "⚽", "minimum": FOOTBALL_MIN_PROBABILITY},
    "soccer_france_ligue_one": {"name": "Futbol - Ligue 1", "emoji": "⚽", "minimum": FOOTBALL_MIN_PROBABILITY},
    "baseball_mlb": {"name": "MLB", "emoji": "⚾", "minimum": MLB_MIN_PROBABILITY},
}


if not BOT_TOKEN:
    raise ValueError("Falta BOT_TOKEN en el archivo .env")
if not ODDS_API_KEY:
    raise ValueError("Falta ODDS_API_KEY en el archivo .env")


def load_state():
    default = {"picks": {}, "subscribers": []}
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
        state.setdefault("picks", {})
        state.setdefault("subscribers", [])
        return state
    except (OSError, json.JSONDecodeError):
        return default


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


STATE = load_state()


def api_get(path, params=None):
    query = {"apiKey": ODDS_API_KEY}
    if params:
        query.update(params)
    response = requests.get(f"{BASE_URL}/{path}", params=query, timeout=25)
    response.raise_for_status()
    return response.json()


def iso_to_local(iso_date):
    try:
        date = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        return date.astimezone(BOT_TIMEZONE).strftime("%d/%m %H:%M")
    except (TypeError, ValueError):
        return iso_date


def best_market_pick(event, sport_key):
    """Devuelve la seleccion con mayor probabilidad implicita sin margen de la casa."""
    markets = []
    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            if market.get("key") == "h2h" and market.get("outcomes"):
                markets.append(market["outcomes"])

    if not markets:
        return None

    # Tomamos la mejor cuota decimal disponible de cada posible resultado.
    best_prices = {}
    for outcomes in markets:
        for outcome in outcomes:
            name = outcome.get("name")
            price = outcome.get("price")
            if name and isinstance(price, (int, float)) and price > 1:
                best_prices[name] = max(best_prices.get(name, 0), price)

    if len(best_prices) < 2:
        return None
    raw = {name: 1 / price for name, price in best_prices.items()}
    total = sum(raw.values())
    if not total:
        return None
    outcome, probability = max(((name, value / total) for name, value in raw.items()), key=lambda item: item[1])
    return {"outcome": outcome, "probability": probability, "odds": best_prices[outcome]}


def is_today_and_future(iso_date):
    try:
        event_time = datetime.fromisoformat(iso_date.replace("Z", "+00:00")).astimezone(BOT_TIMEZONE)
    except (TypeError, ValueError):
        return False
    now = datetime.now(BOT_TIMEZONE)
    return event_time.date() == now.date() and event_time > now


def get_candidates(sport_keys=None):
    candidates = []
    keys = sport_keys or SPORTS.keys()
    for sport_key in keys:
        info = SPORTS[sport_key]
        try:
            events = api_get(f"{sport_key}/odds", {"regions": "us,eu", "markets": "h2h", "oddsFormat": "decimal"})
        except requests.RequestException as error:
            print(f"No se pudieron consultar {sport_key}: {error}")
            continue
        for event in events:
            if not is_today_and_future(event.get("commence_time")):
                continue
            pick = best_market_pick(event, sport_key)
            if pick and pick["probability"] >= info["minimum"]:
                candidates.append({
                    "id": event["id"], "sport_key": sport_key, "league": info["name"], "emoji": info["emoji"],
                    "home_team": event["home_team"], "away_team": event["away_team"], "commence_time": event["commence_time"],
                    **pick,
                })
    return sorted(candidates, key=lambda item: item["probability"], reverse=True)


def pick_message(pick):
    return (
        f"{pick['emoji']} RECOMENDACION\n"
        f"{pick['league']}\n"
        f"{pick['home_team']} vs {pick['away_team']}\n"
        f"Seleccion: {pick['outcome']}\n"
        f"Probabilidad estimada: {pick['probability']:.0%}\n"
        f"Mejor cuota: {pick['odds']:.2f}\n"
        f"Inicio: {iso_to_local(pick['commence_time'])}"
    )


async def send_new_picks(application):
    candidates = await asyncio.to_thread(get_candidates)
    new_picks = [pick for pick in candidates if pick["id"] not in STATE["picks"]][:MAX_PICKS_PER_SCAN]
    if not new_picks:
        return
    for pick in new_picks:
        pick["sent_to"] = list(STATE["subscribers"])
        pick["sent_at"] = datetime.now(timezone.utc).isoformat()
        STATE["picks"][pick["id"]] = pick
        for chat_id in pick["sent_to"]:
            try:
                await application.bot.send_message(chat_id=chat_id, text=pick_message(pick))
            except Exception as error:
                print(f"No se pudo enviar a {chat_id}: {error}")
    save_state(STATE)


def result_for_pick(pick, event):
    scores = {score["name"]: score.get("score") for score in event.get("scores", [])}
    home = scores.get(pick["home_team"])
    away = scores.get(pick["away_team"])
    try:
        home, away = float(home), float(away)
    except (TypeError, ValueError):
        return None
    if home > away:
        winner = pick["home_team"]
    elif away > home:
        winner = pick["away_team"]
    else:
        winner = "Draw"
    won = pick["outcome"] == winner
    return won, f"{int(home) if home.is_integer() else home} - {int(away) if away.is_integer() else away}"


async def check_results(application):
    pending = [pick for pick in STATE["picks"].values() if not pick.get("settled")]
    for pick in pending:
        try:
            events = await asyncio.to_thread(api_get, f"{pick['sport_key']}/scores", {"daysFrom": 3})
        except requests.RequestException as error:
            print(f"No se pudo revisar resultado: {error}")
            continue
        event = next((item for item in events if item.get("id") == pick["id"] and item.get("completed")), None)
        if not event:
            continue
        result = result_for_pick(pick, event)
        if not result:
            continue
        won, score = result
        status = "✅ GANADA" if won else "❌ PERDIDA"
        message = f"{status}\n{pick['home_team']} vs {pick['away_team']}\nTu seleccion: {pick['outcome']}\nResultado: {score}"
        for chat_id in pick.get("sent_to", []):
            try:
                await application.bot.send_message(chat_id=chat_id, text=message)
            except Exception as error:
                print(f"No se pudo enviar resultado a {chat_id}: {error}")
        # Tras avisar el resultado, eliminamos el partido del historial activo.
        # Asi el archivo no acumula recomendaciones ya terminadas.
        STATE["picks"].pop(pick["id"], None)
        save_state(STATE)


async def scan_loop(application):
    last_result_check = None
    while True:
        try:
            now = datetime.now(BOT_TIMEZONE)
            if last_result_check is None or (now - last_result_check).total_seconds() >= RESULT_CHECK_INTERVAL_SECONDS:
                await check_results(application)
                last_result_check = now
            today = now.date().isoformat()
            if now.hour == 8 and now.minute == 0 and STATE.get("last_daily_scan") != today and STATE["subscribers"]:
                await send_new_picks(application)
                STATE["last_daily_scan"] = today
                save_state(STATE)
        except Exception as error:
            print(f"Error en automatizacion: {error}")
        await asyncio.sleep(SCHEDULER_TICK_SECONDS)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in STATE["subscribers"]:
        STATE["subscribers"].append(chat_id)
        save_state(STATE)
    await update.message.reply_text(
        "ValueEdgeBot activado. Revisare Futbol y MLB automaticamente.\n\n"
        "Comandos:\n/best - mejores oportunidades ahora\n/futbol - ver Futbol\n/mlb - ver MLB"
    )


async def show_picks(update: Update, sport_keys=None):
    await update.message.reply_text("Buscando las mejores oportunidades...")
    picks = await asyncio.to_thread(get_candidates, sport_keys)
    if not picks:
        await update.message.reply_text("No hay oportunidades que cumplan el filtro ahora.")
        return
    for pick in picks[:MAX_PICKS_PER_SCAN]:
        await update.message.reply_text(pick_message(pick))


async def best(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_picks(update)


async def futbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_picks(update, [key for key in SPORTS if key.startswith("soccer_")])


async def mlb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_picks(update, ["baseball_mlb"])


async def post_init(application):
    # post_init ocurre antes de que PTB marque la aplicacion como "running".
    # asyncio.create_task evita la advertencia de PTB y guardamos la tarea para cerrarla bien.
    application.bot_data["scan_task"] = asyncio.create_task(scan_loop(application))
    print("Recomendaciones: todos los dias a las 8:00 a. m. (hora de Nueva York).")
    print("Resultados pendientes: cada 10 minutos.")


async def post_shutdown(application):
    task = application.bot_data.get("scan_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("best", best))
app.add_handler(CommandHandler("futbol", futbol))
app.add_handler(CommandHandler("mlb", mlb))

print("ValueEdgeBot iniciado: Futbol + MLB")
app.run_polling()
