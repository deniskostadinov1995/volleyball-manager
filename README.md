# 🏐 Volleyball Manager

**Tools for a recreational volleyball group: fair teams, live stat tracking and player ratings that improve with every game.** It comes in two parts:

- **Telegram bot** (`volleyball_bot.py`): manage the group from chat. Add players, see who's playing, generate balanced teams, track actions and update ratings.
- **Mobile web app** (`web/`): a court-side app for game day. Set up teams, then tap to log every action and point on a live court view.

![Python](https://img.shields.io/badge/python-3.10%2B-3776AB)
![Telegram](https://img.shields.io/badge/Telegram-bot-26A5E4)
![Flask](https://img.shields.io/badge/Flask-3-000000)
![License](https://img.shields.io/badge/license-MIT-green)

The interface is in **Bulgarian**.

## How the rating works

Ratings follow a **DataVolley-inspired** model. Each tracked action (⚡ attack, 🏐 serve, 📥 reception, 🔄 defense, 🛡️ block) is scored by its result. Each skill keeps its own sub-rating, and the overall rating is a weighted combination of them. Ratings are **smoothed** rather than replaced: the weight of new evidence adapts to how many actions a player has, and a player needs at least 5 tracked actions before their rating moves. That way one great (or awful) evening doesn't swing someone's rating wildly. Admins can still set a rating by hand.

Teams are then **auto-balanced by rating**, so games stay close.

---

## Telegram bot

### Commands

| Command | What it does |
|---|---|
| `/start`, `/help` | Welcome and the full command list |
| `/addplayer Ivan` · `/addplayer Ivan, Maria, Petar` | Add one or several players |
| `/players` | All players with their ratings |
| `/stats Ivan` | Detailed statistics for a player |
| `/deleteplayer Ivan` | Remove a player |
| `/setratingmanual Ivan 7.5` | Correct a rating by hand |
| `/playing` · `/whosplaying` | Mark who's playing this week / see the list |
| `/maketeams` | Split today's players into **3 balanced teams** |
| `/cleargame` | Clear everything for a new week |
| `/startgame` · `/track` · `/endgame` | Turn on tracking, log actions live, then finish and update the ratings |
| `/history` | The last 4 weeks |

### Tracking a game

`/track` → pick a player → a **5×3 grid**: each cell is an action *and* its result in a single tap.

| Action | + | = | – |
|---|---|---|---|
| ⚡ Attack | Kill | In play | Error |
| 🏐 Serve | Ace | In play | Error |
| 📥 Reception | Perfect | Good | Poor |
| 🔄 Defense | Save | Touch | Missed |
| 🛡️ Block | Successful | Touch | Missed |

↩️ undoes the last action. Ratings run from **1 to 10** (1–3 beginner · 4–6 intermediate · 7–8 advanced · 9–10 expert).

### Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Install and run:
   ```bash
   git clone https://github.com/deniskostadinov1995/volleyball-manager.git
   cd volleyball-manager
   pip install -r requirements.txt
   ```
   ```powershell
   # Windows: store the token once, then open a new terminal
   setx VOLLEYBALL_BOT_TOKEN "123456789:AA..."
   python volleyball_bot.py
   ```
   ```bash
   # macOS / Linux
   VOLLEYBALL_BOT_TOKEN="123456789:AA..." python volleyball_bot.py
   ```

The bot stores everything in `volleyball_manager.db` (SQLite) next to the script.

---

## Mobile web app

```bash
cd web
python app.py
```

Open **http://localhost:4000** (or `http://<pc-ip>:4000` from phones on the same Wi-Fi).

- **Setup page**: add players, start a game-day session, auto-balance up to **3 teams** by rating (or assign by hand), and set starting positions.
- **Game page**: live court with the score and who's serving. Log each skill action (with direction) and award points, with undo. Finish matches, then close the day to save the new ratings.
- **Summary**: per-player stats for the session.

Data lives in `web/volleyball.db`, separate from the bot's database.

### API

| Method & path | Purpose |
|---|---|
| `GET/POST /api/players`, `PUT/DELETE /api/players/<id>` | Manage players |
| `POST /api/session/start` · `GET /api/session/state` | Game-day session |
| `POST /api/session/teams` · `/auto-teams` · `/positions` | Teams and starting positions |
| `GET /api/game/court` | Current court, score, server |
| `POST /api/game/log` · `/point` · `/undo-point` · `/end-match` · `/end-day` | Live game |
| `GET /api/game/summary` | Per-player stats for the session |

## License

[MIT](LICENSE) © Denis Kostadinov
