# ValueEdgeBot

Telegram bot that ranks market-based MLB, NBA, and soccer selections. It uses
SportsGameOdds as its primary source, keeps Odds-API.io as an optional fallback,
and sends the daily report at 8:00 AM in `America/New_York`.

## Commands

- `/mlb`, `/nba`, `/futbol`: up to the five strongest qualifying selections.
- `/parlay`: up to ten qualifying MLB player props, ranked by ValueEdge score.
- `/best`: the strongest qualifying selections across all three sports.

The bot never pads a list with picks that fail its quality filters. Result
notifications remain `GANADA` or `PERDIDA` after an event is finalized.

## Configuration

Copy `.env.example` to `.env` for local use. Never commit `.env` or API keys.

Required:

- `BOT_TOKEN`: token from BotFather.
- `SPORTSGAMEODDS_API_KEY`: key from the SportsGameOdds Amateur plan. If this
  is omitted, `ODDS_API_KEY` becomes required as the legacy fallback.

Optional:

- `TELEGRAM_CHAT_ID`: chat that should receive the daily report before anyone
  runs `/start`. Every chat that runs `/start` is also subscribed.
- `ODDS_API_KEY`: Odds-API.io fallback if SportsGameOdds is unavailable or not
  configured.
- `API_SPORTS_KEY`: existing independent confirmation layer.
- `SPORTSGAMEODDS_BOOKMAKERS`: defaults to `draftkings,fanduel`.
- `SPORTSGAMEODDS_CACHE_SECONDS`: defaults to 600 seconds, matching the free
  plan's update frequency and reducing monthly object usage.

For GitHub Actions, add the same values under **Settings → Secrets and
variables → Actions**. At minimum add `BOT_TOKEN`,
`SPORTSGAMEODDS_API_KEY`, and `TELEGRAM_CHAT_ID` if no user will run `/start`.

## Free-plan scope

The SportsGameOdds Amateur plan currently lists MLB, NBA, MLS, and Champions
League among its eight leagues and includes DraftKings/FanDuel, player props,
scores, and results. PrizePicks is not listed on that free tier, so `/parlay`
uses available DraftKings/FanDuel lines. Soccer totals are used normally; BTTS
is displayed only if the provider actually returns that market.

## Run locally

```powershell
python -m pip install -r requirements.txt
python bot.py
```

The in-process scheduler checks the New York clock and sends during the
8:00–8:14 AM window, once per calendar day. The narrow window also prevents a
later restart of GitHub's temporary runner from duplicating the daily report.
The included workflow restarts Telegram polling every four hours.
