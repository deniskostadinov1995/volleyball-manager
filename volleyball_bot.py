"""
Volleyball Manager Pro v4
DataVolley-inspired rating system with improved live-tracking UX

Changes from v3:
- TRACK UX: Combined action+result grid (2 taps instead of 3 per action)
- TRACK UX: Live session stats shown while tracking
- TRACK UX: Last-tracked player pinned to top of list
- TRACK UX: Undo last action button
- RATING FIX: Removed incorrect 1.2x attack multiplier (was pushing attack_rating > 10)
- RATING FIX: Individual skill sub-ratings now smoothed, not fully replaced
- RATING FIX: Adaptive smoothing weight based on total actions tracked
- RATING FIX: Minimum 5 actions required to update rating
- RATING FIX: games_played now incremented in /endgame, not /maketeams
- BUG FIX: /addplayer now correctly handles comma-separated names
- API token read from VOLLEYBALL_BOT_TOKEN env variable
- Added /setratingmanual for admin rating adjustments
"""

import sqlite3
import logging
import os
from datetime import datetime
from typing import List, Tuple, Dict, Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────
# Token from @BotFather, read from the VOLLEYBALL_BOT_TOKEN environment variable.
BOT_TOKEN = os.environ.get('VOLLEYBALL_BOT_TOKEN', '')
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'volleyball_manager.db')

# Action grid order shown to tracker — put most-common actions first
ACTIONS = [
    ('attack',    '⚡', 'Атака'),
    ('serve',     '🏐', 'Серв'),
    ('reception', '📥', 'Прием'),
    ('defense',   '🔄', 'Защита'),
    ('block',     '🛡️', 'Блок'),
]

SKILL_WEIGHTS = {
    'serve':     0.15,
    'attack':    0.30,
    'block':     0.20,
    'reception': 0.20,
    'defense':   0.15,
}


# ── Database ─────────────────────────────────────────────────────────────────

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db():
    logger.info(f"Initializing database at: {DB_PATH}")
    conn = get_db_connection()
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS players (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        rating REAL NOT NULL DEFAULT 5.0,
        serve_rating REAL DEFAULT 5.0,
        attack_rating REAL DEFAULT 5.0,
        block_rating REAL DEFAULT 5.0,
        reception_rating REAL DEFAULT 5.0,
        defense_rating REAL DEFAULT 5.0,
        games_played INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS current_game (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id INTEGER,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (player_id) REFERENCES players(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS game_performance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id INTEGER,
        game_date DATE,
        team_number INTEGER DEFAULT 0,
        serve_plus INTEGER DEFAULT 0,
        serve_neutral INTEGER DEFAULT 0,
        serve_minus INTEGER DEFAULT 0,
        attack_plus INTEGER DEFAULT 0,
        attack_neutral INTEGER DEFAULT 0,
        attack_minus INTEGER DEFAULT 0,
        block_plus INTEGER DEFAULT 0,
        block_neutral INTEGER DEFAULT 0,
        block_minus INTEGER DEFAULT 0,
        reception_plus INTEGER DEFAULT 0,
        reception_neutral INTEGER DEFAULT 0,
        reception_minus INTEGER DEFAULT 0,
        defense_plus INTEGER DEFAULT 0,
        defense_neutral INTEGER DEFAULT 0,
        defense_minus INTEGER DEFAULT 0,
        performance_score REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (player_id) REFERENCES players(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS rating_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id INTEGER,
        old_rating REAL,
        new_rating REAL,
        changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (player_id) REFERENCES players(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS teams (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        team_number INTEGER,
        player_id INTEGER,
        week_date DATE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (player_id) REFERENCES players(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id INTEGER,
        game_date DATE,
        FOREIGN KEY (player_id) REFERENCES players(id),
        UNIQUE(player_id, game_date)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS active_game (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        game_date DATE,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    conn.commit()
    conn.close()
    logger.info("Database initialized.")


# ── Rating calculations ───────────────────────────────────────────────────────

def calculate_skill_score(plus: int, neutral: int, minus: int) -> float:
    """
    Score a skill on a 0–10 scale using DataVolley weighting.
    plus=3pts, neutral=1pt, minus=-2pts (same as DataVolley efficiency).
    No extra multiplier here — weighting between skills is done in
    calculate_overall_rating() via SKILL_WEIGHTS.
    """
    total = plus + neutral + minus
    if total == 0:
        return 5.0  # default when no data

    weighted = plus * 3 + neutral * 1 + minus * (-2)
    max_possible = total * 3
    min_possible = total * (-2)
    normalized = (weighted - min_possible) / (max_possible - min_possible) * 10
    return round(normalized, 2)


def calculate_overall_rating(skill_ratings: Dict[str, float]) -> float:
    """Weighted average of all skill scores → overall 0–10 rating."""
    total = sum(skill_ratings.get(s, 5.0) * w for s, w in SKILL_WEIGHTS.items())
    return round(total, 2)


def adaptive_weight(total_actions: int) -> float:
    """
    How much weight to give the current game's data.
    Fewer actions → less trust → smaller update.
    This prevents wild swings from 2–3 logged actions.
    """
    if total_actions < 5:
        return 0.0   # not enough data — skip update
    elif total_actions < 10:
        return 0.20  # small sample
    elif total_actions < 20:
        return 0.30  # medium sample
    else:
        return 0.40  # good sample — trust it more


def smooth(new_val: float, old_val: float, weight: float) -> float:
    return round(new_val * weight + old_val * (1 - weight), 2)


# ── Team balancing ────────────────────────────────────────────────────────────

def create_balanced_teams(players: List[Tuple], num_teams: int = 3) -> Tuple[List[List[Tuple]], List[float]]:
    """Greedy snake-draft balancing: sort by rating, assign each player to the weakest team."""
    sorted_players = sorted(players, key=lambda x: x[1], reverse=True)
    teams = [[] for _ in range(num_teams)]
    strengths = [0.0] * num_teams
    for player in sorted_players:
        idx = strengths.index(min(strengths))
        teams[idx].append(player)
        strengths[idx] += player[1]
    return teams, strengths


# ── Tracking helpers ──────────────────────────────────────────────────────────

def get_session_stats(c, player_id: int, game_date: str) -> Dict[str, Tuple[int, int, int]]:
    """Return per-action (plus, neutral, minus) counts for this game session."""
    c.execute('''SELECT
                 serve_plus, serve_neutral, serve_minus,
                 attack_plus, attack_neutral, attack_minus,
                 block_plus, block_neutral, block_minus,
                 reception_plus, reception_neutral, reception_minus,
                 defense_plus, defense_neutral, defense_minus
                 FROM game_performance
                 WHERE player_id = ? AND game_date = ?''', (player_id, game_date))
    row = c.fetchone()
    if not row:
        return {a: (0, 0, 0) for a, _, _ in ACTIONS}
    return {
        'serve':     (row[0],  row[1],  row[2]),
        'attack':    (row[3],  row[4],  row[5]),
        'block':     (row[6],  row[7],  row[8]),
        'reception': (row[9],  row[10], row[11]),
        'defense':   (row[12], row[13], row[14]),
    }


def build_action_grid(player_id: int, stats: Dict[str, Tuple[int, int, int]]) -> List:
    """
    Build the combined action+result keyboard.
    One row per action, three columns: + / = / –
    Shows current counts inline so the tracker can verify.
    """
    keyboard = []
    for action, icon, label in ACTIONS:
        p, n, m = stats.get(action, (0, 0, 0))
        count_str = f" {p}+{n}={m}-" if (p + n + m) > 0 else ""
        keyboard.append([
            InlineKeyboardButton(f"{icon}+ {label}{count_str}",
                                 callback_data=f"score_{player_id}_{action}_plus"),
            InlineKeyboardButton(f"{icon}= {label}",
                                 callback_data=f"score_{player_id}_{action}_neutral"),
            InlineKeyboardButton(f"{icon}– {label}",
                                 callback_data=f"score_{player_id}_{action}_minus"),
        ])
    keyboard.append([
        InlineKeyboardButton("↩️ Отмени", callback_data="undo_last"),
        InlineKeyboardButton("⬅️ Играчи", callback_data="track_back"),
    ])
    return keyboard


def format_session_summary(stats: Dict[str, Tuple[int, int, int]]) -> str:
    """Compact one-line summary of all tracked actions for a player."""
    parts = []
    for action, icon, _ in ACTIONS:
        p, n, m = stats.get(action, (0, 0, 0))
        if p + n + m > 0:
            parts.append(f"{icon}{p}+{n}={m}-")
    return "  ".join(parts) if parts else "_(нищо маркирано)_"


def total_actions_tracked(stats: Dict[str, Tuple[int, int, int]]) -> int:
    return sum(p + n + m for p, n, m in stats.values())


# ── Command handlers ──────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🏐 *Волейбол Мениджър Pro v4*\n"
        "_DataVolley рейтинг система_\n\n"
        "*Бърз старт:*\n"
        r"1\. `/addplayer Иван` или `/addplayer Иван, Мария, Петър`" "\n"
        r"2\. `/playing` \-\- избери кой играе" "\n"
        r"3\. `/maketeams` \-\- генерирай 3 отбора" "\n"
        r"4\. `/startgame` \-\- активирай следенето" "\n"
        r"5\. `/track` \-\- маркирай действия LIVE" "\n"
        r"6\. `/endgame` \-\- приключи и обнови рейтинги" "\n\n"
        r"`/help` за пълен списък команди\."
    )
    await update.message.reply_text(text, parse_mode='MarkdownV2')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📋 *Команди*\n\n"
        "*Играчи:*\n"
        "`/addplayer Иван` — добави играч\n"
        "`/addplayer Иван, Мария, Петър` — добави няколко\n"
        "`/players` — всички играчи и рейтинги\n"
        "`/stats Иван` — детайлна статистика\n"
        "`/deleteplayer Иван` — изтрий играч\n"
        "`/setratingmanual Иван 7.5` — ръчна корекция на рейтинг\n\n"
        "*Седмична игра:*\n"
        "`/playing` — маркирай кой играе\n"
        "`/whosplaying` — виж маркираните\n"
        "`/maketeams` — 3 балансирани отбора\n"
        "`/cleargame` — изчисти за нова седмица\n\n"
        "*По време на игра:*\n"
        "`/startgame` — активирай следене\n"
        "`/track` — маркирай действия LIVE\n"
        "`/endgame` — приключи и обнови рейтинги\n\n"
        "*История:*\n"
        "`/history` — последните 4 седмици\n\n"
        "*Как се следи:*\n"
        "• `/track` → избери играч → виж решетка 5×3\n"
        "• Всяка клетка = действие + резултат в 1 клик\n"
        "• ⚡ Атака: + Кил / = В игра / – Грешка\n"
        "• 🏐 Сервис: + Ас / = В игра / – Грешка\n"
        "• 📥 Прием: + Перфект / = Добър / – Слаб\n"
        "• 🔄 Защита: + Спасяване / = Допир / – Пропуснат\n"
        "• 🛡️ Блок: + Успешен / = Допир / – Пропуснат\n"
        "• Бутонът ↩️ отменя последното действие\n\n"
        "*Рейтинг скала:* 1–3 Начинаещ · 4–6 Среден · 7–8 Напреднал · 9–10 Експерт"
    )
    await update.message.reply_text(text, parse_mode='Markdown')


async def add_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Използване: `/addplayer Иван` или `/addplayer Иван, Мария, Петър`",
            parse_mode='Markdown'
        )
        return

    full_text = ' '.join(context.args)
    # Support both comma-separated and single name
    names = [n.strip() for n in full_text.split(',') if n.strip()]

    conn = get_db_connection()
    c = conn.cursor()
    added, skipped = [], []

    for name in names:
        try:
            c.execute('INSERT INTO players (name, rating) VALUES (?, 5.0)', (name,))
            added.append(name)
        except sqlite3.IntegrityError:
            skipped.append(name)

    conn.commit()
    conn.close()

    msg = ""
    if added:
        msg += f"✅ Добавени: {', '.join(f'**{n}**' for n in added)} (рейтинг 5.0)\n"
    if skipped:
        msg += f"⚠️ Вече съществуват: {', '.join(skipped)}"
    await update.message.reply_text(msg.strip(), parse_mode='Markdown')


async def list_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''SELECT name, rating, serve_rating, attack_rating,
                 block_rating, reception_rating, defense_rating, games_played
                 FROM players ORDER BY rating DESC''')
    players = c.fetchall()
    conn.close()

    if not players:
        await update.message.reply_text("Няма играчи! Добавете с /addplayer")
        return

    msg = f"👥 **Всички играчи ({len(players)}):**\n\n"
    for name, rating, srv, atk, blk, rec, dfs, games in players:
        msg += f"**{name}** — {rating:.1f}/10 ({games} игри)\n"
        msg += f"  Серв:{srv:.1f}  Атк:{atk:.1f}  Блок:{blk:.1f}  Прием:{rec:.1f}  Защ:{dfs:.1f}\n\n"

    await update.message.reply_text(msg, parse_mode='Markdown')


async def delete_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Използване: `/deleteplayer Иван`", parse_mode='Markdown'
        )
        return

    name = ' '.join(context.args)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('DELETE FROM players WHERE name = ?', (name,))
    if c.rowcount > 0:
        conn.commit()
        await update.message.reply_text(f"🗑️ Изтрит **{name}**.", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ '{name}' не е намерен!")
    conn.close()


async def set_rating_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /setratingmanual Иван 7.5"""
    if len(context.args) < 2:
        await update.message.reply_text(
            "Използване: `/setratingmanual Иван 7.5`\n"
            "Задава ръчно рейтинга и всички под-рейтинги на даден играч.",
            parse_mode='Markdown'
        )
        return

    try:
        new_rating = float(context.args[-1])
        name = ' '.join(context.args[:-1])
    except ValueError:
        await update.message.reply_text("❌ Невалиден рейтинг. Пример: `/setratingmanual Иван 7.5`",
                                        parse_mode='Markdown')
        return

    if not (1.0 <= new_rating <= 10.0):
        await update.message.reply_text("❌ Рейтингът трябва да е между 1.0 и 10.0")
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT id, rating FROM players WHERE name = ?', (name,))
    result = c.fetchone()
    if not result:
        conn.close()
        await update.message.reply_text(f"❌ '{name}' не е намерен!")
        return

    player_id, old_rating = result
    c.execute('''UPDATE players SET
                 rating = ?, serve_rating = ?, attack_rating = ?,
                 block_rating = ?, reception_rating = ?, defense_rating = ?,
                 updated_at = CURRENT_TIMESTAMP WHERE id = ?''',
              (new_rating, new_rating, new_rating, new_rating, new_rating, new_rating, player_id))
    c.execute('INSERT INTO rating_history (player_id, old_rating, new_rating) VALUES (?, ?, ?)',
              (player_id, old_rating, new_rating))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ **{name}**: {old_rating:.1f} → {new_rating:.1f} (ръчна корекция)",
        parse_mode='Markdown'
    )


async def player_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Използване: `/stats Иван`", parse_mode='Markdown'
        )
        return

    name = ' '.join(context.args)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''SELECT id, rating, serve_rating, attack_rating, block_rating,
                 reception_rating, defense_rating, games_played, created_at
                 FROM players WHERE name = ?''', (name,))
    result = c.fetchone()

    if not result:
        conn.close()
        await update.message.reply_text(f"❌ '{name}' не е намерен!")
        return

    player_id, rating, srv, atk, blk, rec, dfs, games, created_at = result

    c.execute('''SELECT old_rating, new_rating, changed_at
                 FROM rating_history WHERE player_id = ?
                 ORDER BY changed_at DESC LIMIT 5''', (player_id,))
    history = c.fetchall()

    c.execute('''SELECT new_rating FROM rating_history WHERE player_id = ?
                 ORDER BY changed_at ASC LIMIT 1''', (player_id,))
    first = c.fetchone()
    initial = first[0] if first else 5.0
    conn.close()

    change = rating - initial
    trend = "📈" if change > 0.05 else "📉" if change < -0.05 else "➡️"

    msg = f"📊 **Статистика: {name}**\n\n"
    msg += f"⭐ Рейтинг: **{rating:.1f}/10** ({games} игри)\n"
    msg += f"{trend} Промяна от начало: {change:+.1f}\n\n"
    msg += "**Умения:**\n"
    msg += f"⚡ Атака:  {atk:.1f}/10\n"
    msg += f"🏐 Сервис: {srv:.1f}/10\n"
    msg += f"📥 Прием:  {rec:.1f}/10\n"
    msg += f"🔄 Защита: {dfs:.1f}/10\n"
    msg += f"🛡️ Блок:   {blk:.1f}/10\n"

    if history:
        msg += "\n**Последни промени:**\n"
        for old, new, changed in history[:4]:
            diff = new - old
            arrow = "↗️" if diff > 0 else "↘️" if diff < 0 else "→"
            try:
                date_str = datetime.strptime(changed, '%Y-%m-%d %H:%M:%S').strftime('%d.%m.%Y')
            except ValueError:
                date_str = changed[:10]
            msg += f"{arrow} {date_str}: {old:.1f} → {new:.1f} ({diff:+.1f})\n"

    await update.message.reply_text(msg, parse_mode='Markdown')


async def select_playing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT id, name, rating FROM players ORDER BY name')
    players = c.fetchall()
    c.execute('SELECT player_id FROM current_game')
    selected = {row[0] for row in c.fetchall()}
    conn.close()

    if not players:
        await update.message.reply_text("Няма играчи! Добавете с /addplayer")
        return

    keyboard = []
    for pid, name, rating in players:
        box = "✅" if pid in selected else "☐"
        keyboard.append([InlineKeyboardButton(
            f"{box} {name} ({rating:.1f})", callback_data=f"toggle_{pid}"
        )])
    keyboard.append([InlineKeyboardButton("✔️ Готово", callback_data="done_selecting")])

    await update.message.reply_text(
        "👥 **Изберете кой играе тази неделя:**\n"
        "Натиснете за добавяне/премахване",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


async def whos_playing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''SELECT p.name, p.rating FROM current_game cg
                 JOIN players p ON cg.player_id = p.id
                 ORDER BY p.rating DESC''')
    players = c.fetchall()
    conn.close()

    if not players:
        await update.message.reply_text(
            "Никой не е маркиран!\nИзползвайте /playing."
        )
        return

    msg = f"🏐 **Играят тази неделя ({len(players)} играчи):**\n\n"
    for name, rating in players:
        msg += f"• {name} — {rating:.1f}/10\n"
    avg = sum(r for _, r in players) / len(players)
    msg += f"\n📊 Среден рейтинг: {avg:.1f}/10"
    await update.message.reply_text(msg, parse_mode='Markdown')


async def make_teams(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''SELECT p.id, p.name, p.rating FROM current_game cg
                 JOIN players p ON cg.player_id = p.id
                 ORDER BY p.rating DESC''')
    players = c.fetchall()

    if len(players) < 6:
        conn.close()
        await update.message.reply_text(
            f"❌ Само {len(players)} избрани. Нужни са поне 6. Използвайте /playing."
        )
        return

    game_date = datetime.now().date().isoformat()

    # Record attendance (do NOT increment games_played here — that happens in /endgame)
    for pid, _, _ in players:
        c.execute('INSERT OR IGNORE INTO attendance (player_id, game_date) VALUES (?, ?)',
                  (pid, game_date))

    player_data = [(name, rating) for _, name, rating in players]
    teams, strengths = create_balanced_teams(player_data, num_teams=3)

    # Save team composition to history
    for team_num, team in enumerate(teams, 1):
        for name, _ in team:
            c.execute('SELECT id FROM players WHERE name = ?', (name,))
            pid = c.fetchone()[0]
            c.execute('INSERT INTO teams (team_number, player_id, week_date) VALUES (?, ?, ?)',
                      (team_num, pid, game_date))

    conn.commit()
    conn.close()

    msg = "🏐 **ОТБОРИ ЗА ТАЗИ НЕДЕЛЯ** 🏐\n\n"
    for i, (team, strength) in enumerate(zip(teams, strengths), 1):
        msg += f"**Отбор {i}** (Сила: {strength:.1f}):\n"
        for j, (name, rating) in enumerate(team, 1):
            msg += f"  {j}. {name} ({rating:.1f})\n"
        msg += "\n"

    avg = sum(strengths) / len(strengths)
    max_diff = max(abs(s - avg) for s in strengths)
    msg += f"📊 Баланс: {max_diff:.2f} (по-ниско = по-добре)\n"
    msg += f"⚖️ Средна сила: {avg:.1f}\n\n"
    msg += "💡 Използвайте /startgame за следене."
    await update.message.reply_text(msg, parse_mode='Markdown')


async def clear_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('DELETE FROM current_game')
    conn.commit()
    conn.close()
    await update.message.reply_text(
        "🗑️ Тази седмица е изчистена!\nИзползвайте /playing за следващата игра."
    )


async def team_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT DISTINCT week_date FROM teams ORDER BY week_date DESC LIMIT 4')
    dates = [row[0] for row in c.fetchall()]

    if not dates:
        conn.close()
        await update.message.reply_text("Все още няма история!\nИзползвайте /maketeams.")
        return

    msg = "📜 **История (Последни 4 седмици)**\n\n"
    for date in dates:
        date_str = datetime.strptime(date, '%Y-%m-%d').strftime('%d.%m.%Y')
        msg += f"**{date_str}:**\n"
        for team_num in [1, 2, 3]:
            c.execute('''SELECT p.name FROM teams t
                         JOIN players p ON t.player_id = p.id
                         WHERE t.week_date = ? AND t.team_number = ?
                         ORDER BY p.rating DESC''', (date, team_num))
            tp = [r[0] for r in c.fetchall()]
            if tp:
                msg += f"  Отбор {team_num}: {', '.join(tp)}\n"
        msg += "\n"

    conn.close()
    await update.message.reply_text(msg, parse_mode='Markdown')


async def start_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    c = conn.cursor()

    c.execute('SELECT is_active FROM active_game WHERE id = 1')
    result = c.fetchone()
    if result and result[0] == 1:
        conn.close()
        await update.message.reply_text(
            "⚠️ Има активна игра!\nИзползвайте /track за следене или /endgame за приключване."
        )
        return

    c.execute('SELECT player_id FROM current_game')
    player_ids = [row[0] for row in c.fetchall()]
    if not player_ids:
        conn.close()
        await update.message.reply_text("❌ Няма избрани играчи!\nИзползвайте /playing първо.")
        return

    game_date = datetime.now().date().isoformat()
    c.execute('INSERT OR REPLACE INTO active_game (id, game_date, is_active) VALUES (1, ?, 1)',
              (game_date,))

    for pid in player_ids:
        # Check if performance row already exists (in case /startgame is called again same day)
        c.execute('SELECT id FROM game_performance WHERE player_id = ? AND game_date = ?',
                  (pid, game_date))
        if not c.fetchone():
            c.execute('INSERT INTO game_performance (player_id, game_date, team_number) VALUES (?, ?, 0)',
                      (pid, game_date))

    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"🏐 **ИГРА ЗАПОЧНАТА!**\n\n"
        f"Дата: {game_date}\n"
        f"Играчи: {len(player_ids)}\n\n"
        "Използвайте /track за маркиране на действия!",
        parse_mode='Markdown'
    )


async def end_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    End the current game session and update ratings.

    Rating update logic:
    - Requires at least 5 total tracked actions to update a player's rating.
    - Adaptive smoothing: more actions tracked → more weight to new data.
    - Each skill sub-rating is smoothed individually (old data not discarded).
    - Skills with 0 actions tracked this session are NOT changed.
    """
    conn = get_db_connection()
    c = conn.cursor()

    c.execute('SELECT game_date FROM active_game WHERE id = 1 AND is_active = 1')
    result = c.fetchone()
    if not result:
        conn.close()
        await update.message.reply_text("❌ Няма активна игра!")
        return

    game_date = result[0]

    c.execute('''SELECT player_id,
                 serve_plus, serve_neutral, serve_minus,
                 attack_plus, attack_neutral, attack_minus,
                 block_plus, block_neutral, block_minus,
                 reception_plus, reception_neutral, reception_minus,
                 defense_plus, defense_neutral, defense_minus
                 FROM game_performance WHERE game_date = ?''', (game_date,))
    performances = c.fetchall()

    msg = "🏁 **ИГРА ЗАВЪРШЕНА!**\n\n📊 **Рейтинги:**\n\n"
    updated, skipped = 0, 0

    for perf in performances:
        player_id = perf[0]
        serves    = (perf[1],  perf[2],  perf[3])
        attacks   = (perf[4],  perf[5],  perf[6])
        blocks    = (perf[7],  perf[8],  perf[9])
        receptions= (perf[10], perf[11], perf[12])
        defenses  = (perf[13], perf[14], perf[15])

        skill_data = {
            'serve':     serves,
            'attack':    attacks,
            'block':     blocks,
            'reception': receptions,
            'defense':   defenses,
        }
        total_actions = sum(sum(v) for v in skill_data.values())
        weight = adaptive_weight(total_actions)

        c.execute('''SELECT name, rating,
                     serve_rating, attack_rating, block_rating,
                     reception_rating, defense_rating
                     FROM players WHERE id = ?''', (player_id,))
        name, old_rating, old_srv, old_atk, old_blk, old_rec, old_dfs = c.fetchone()
        old_skills = {
            'serve': old_srv, 'attack': old_atk, 'block': old_blk,
            'reception': old_rec, 'defense': old_dfs,
        }

        if weight == 0.0:
            msg += f"⏭️ **{name}**: пропуснат ({total_actions} действия — нужни поне 5)\n"
            skipped += 1
            continue

        # Smooth each skill sub-rating only if there were actions for it this session
        new_skills = {}
        for skill, counts in skill_data.items():
            if sum(counts) >= 2:
                game_score = calculate_skill_score(*counts)
                new_skills[skill] = smooth(game_score, old_skills[skill], weight)
            else:
                # No meaningful data for this skill — keep old value unchanged
                new_skills[skill] = old_skills[skill]

        new_overall = calculate_overall_rating(new_skills)
        change = round(new_overall - old_rating, 2)
        arrow = "📈" if change > 0.05 else "📉" if change < -0.05 else "➡️"

        c.execute('INSERT INTO rating_history (player_id, old_rating, new_rating) VALUES (?, ?, ?)',
                  (player_id, old_rating, new_overall))
        c.execute('''UPDATE players SET
                     rating = ?, serve_rating = ?, attack_rating = ?,
                     block_rating = ?, reception_rating = ?, defense_rating = ?,
                     games_played = games_played + 1,
                     updated_at = CURRENT_TIMESTAMP
                     WHERE id = ?''',
                  (new_overall, new_skills['serve'], new_skills['attack'],
                   new_skills['block'], new_skills['reception'], new_skills['defense'],
                   player_id))
        c.execute('UPDATE game_performance SET performance_score = ? WHERE player_id = ? AND game_date = ?',
                  (new_overall, player_id, game_date))

        msg += f"{arrow} **{name}**: {old_rating:.1f} → {new_overall:.1f} ({change:+.1f}) | {total_actions} действия\n"
        updated += 1

    c.execute('UPDATE active_game SET is_active = 0 WHERE id = 1')
    conn.commit()
    conn.close()

    msg += f"\n✅ {updated} обновени"
    if skipped:
        msg += f", {skipped} пропуснати (малко данни)"
    msg += "\n\nИзползвайте /cleargame за нова седмица."
    await update.message.reply_text(msg, parse_mode='Markdown')


# ── Live tracking command & callbacks ─────────────────────────────────────────

async def track_performance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open the player-selection list for live tracking."""
    conn = get_db_connection()
    c = conn.cursor()

    c.execute('SELECT game_date, is_active FROM active_game WHERE id = 1')
    result = c.fetchone()
    if not result or result[1] != 1:
        conn.close()
        await update.message.reply_text(
            "❌ Няма активна игра!\nЗапочнете с /startgame."
        )
        return

    game_date = result[0]
    c.execute('''SELECT p.id, p.name, p.rating FROM current_game cg
                 JOIN players p ON cg.player_id = p.id
                 ORDER BY p.name''')
    players = c.fetchall()

    # Collect total tracked actions per player for display
    player_totals = {}
    for pid, _, _ in players:
        stats = get_session_stats(c, pid, game_date)
        player_totals[pid] = total_actions_tracked(stats)

    conn.close()

    if not players:
        await update.message.reply_text("Няма играчи в играта!")
        return

    # Put the last-tracked player at the top
    last_id = context.user_data.get('last_tracked_id')

    def sort_key(p):
        return (0 if p[0] == last_id else 1, p[1])

    players_sorted = sorted(players, key=sort_key)

    keyboard = []
    for pid, name, rating in players_sorted:
        total = player_totals[pid]
        suffix = f" [{total}]" if total > 0 else ""
        pin = "⚡ " if pid == last_id else ""
        keyboard.append([InlineKeyboardButton(
            f"{pin}{name} ({rating:.1f}){suffix}",
            callback_data=f"track_player_{pid}"
        )])
    keyboard.append([InlineKeyboardButton("✔️ Готово", callback_data="track_done")])

    await update.message.reply_text(
        "📊 **СЛЕДЕНЕ НА ДЕЙСТВИЯ**\n\n"
        "Изберете играч:\n"
        "_⚡ = последен · [N] = брой маркирани_",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


async def track_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle all /track-related callbacks.

    Navigation model:
    track_back           → player list
    track_player_{id}    → player action grid (combined action+result in one tap)
    score_{id}_{a}_{r}   → log action, stay on player grid with updated stats
    undo_last            → reverse last logged action, stay on player grid
    track_done           → close tracker
    """
    query = update.callback_query
    await query.answer()

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT game_date FROM active_game WHERE id = 1 AND is_active = 1')
    ag = c.fetchone()

    if not ag:
        conn.close()
        await query.edit_message_text("❌ Игра не е активна. Използвайте /startgame.")
        return

    game_date = ag[0]

    # ── Done ──────────────────────────────────────────────────────────────────
    if query.data == "track_done":
        conn.close()
        await query.edit_message_text(
            "✅ Следенето продължава!\n\n"
            "Използвайте /track отново или /endgame за приключване."
        )
        return

    # ── Player list (back button) ──────────────────────────────────────────────
    if query.data == "track_back":
        c.execute('''SELECT p.id, p.name, p.rating FROM current_game cg
                     JOIN players p ON cg.player_id = p.id ORDER BY p.name''')
        players = c.fetchall()

        player_totals = {}
        for pid, _, _ in players:
            stats = get_session_stats(c, pid, game_date)
            player_totals[pid] = total_actions_tracked(stats)

        conn.close()

        last_id = context.user_data.get('last_tracked_id')

        def sort_key(p):
            return (0 if p[0] == last_id else 1, p[1])

        players_sorted = sorted(players, key=sort_key)
        keyboard = []
        for pid, name, rating in players_sorted:
            total = player_totals[pid]
            suffix = f" [{total}]" if total > 0 else ""
            pin = "⚡ " if pid == last_id else ""
            keyboard.append([InlineKeyboardButton(
                f"{pin}{name} ({rating:.1f}){suffix}",
                callback_data=f"track_player_{pid}"
            )])
        keyboard.append([InlineKeyboardButton("✔️ Готово", callback_data="track_done")])

        await query.edit_message_text(
            "📊 **СЛЕДЕНЕ НА ДЕЙСТВИЯ**\n\n"
            "Изберете играч:\n"
            "_⚡ = последен · [N] = брой маркирани_",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return

    # ── Player action grid ─────────────────────────────────────────────────────
    if query.data.startswith("track_player_"):
        player_id = int(query.data.split("_")[2])
        context.user_data['last_tracked_id'] = player_id

        c.execute('SELECT name, rating FROM players WHERE id = ?', (player_id,))
        name, rating = c.fetchone()
        stats = get_session_stats(c, player_id, game_date)
        conn.close()

        total = total_actions_tracked(stats)
        summary = format_session_summary(stats)

        keyboard = build_action_grid(player_id, stats)
        await query.edit_message_text(
            f"📊 **{name}** ({rating:.1f}/10)\n\n"
            f"{summary}\n\n"
            "Маркирайте действие  _(ред = действие · колона = +/=/–)_:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return

    # ── Log action ─────────────────────────────────────────────────────────────
    if query.data.startswith("score_"):
        parts = query.data.split("_")
        player_id  = int(parts[1])
        action     = parts[2]
        score_type = parts[3]

        # Validate action and score_type to avoid any SQL issues
        valid_actions = {'serve', 'attack', 'block', 'reception', 'defense'}
        valid_scores  = {'plus', 'neutral', 'minus'}
        if action not in valid_actions or score_type not in valid_scores:
            conn.close()
            return

        field = f"{action}_{score_type}"
        c.execute(f'UPDATE game_performance SET {field} = {field} + 1 WHERE player_id = ? AND game_date = ?',
                  (player_id, game_date))

        # Store undo context
        context.user_data['last_action'] = {
            'player_id': player_id,
            'field': field,
            'game_date': game_date,
        }
        context.user_data['last_tracked_id'] = player_id

        c.execute('SELECT name, rating FROM players WHERE id = ?', (player_id,))
        name, rating = c.fetchone()
        stats = get_session_stats(c, player_id, game_date)
        conn.commit()
        conn.close()

        score_emoji = {"plus": "✅ +", "neutral": "➖ =", "minus": "❌ –"}
        action_bg = {'serve': '🏐', 'attack': '⚡', 'block': '🛡️', 'reception': '📥', 'defense': '🔄'}
        await query.answer(f"{score_emoji[score_type]} {action_bg[action]}")

        summary = format_session_summary(stats)
        keyboard = build_action_grid(player_id, stats)
        await query.edit_message_text(
            f"📊 **{name}** ({rating:.1f}/10)\n\n"
            f"{summary}\n\n"
            "Маркирайте действие  _(ред = действие · колона = +/=/–)_:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return

    # ── Undo last action ───────────────────────────────────────────────────────
    if query.data == "undo_last":
        last = context.user_data.get('last_action')
        if not last:
            conn.close()
            await query.answer("Няма какво да се отмени!", show_alert=True)
            return

        player_id = last['player_id']
        field     = last['field']
        ld        = last['game_date']

        # Decrement but never go below 0
        c.execute(f'''UPDATE game_performance
                     SET {field} = MAX(0, {field} - 1)
                     WHERE player_id = ? AND game_date = ?''',
                  (player_id, ld))

        context.user_data.pop('last_action', None)

        c.execute('SELECT name, rating FROM players WHERE id = ?', (player_id,))
        name, rating = c.fetchone()
        stats = get_session_stats(c, player_id, ld)
        conn.commit()
        conn.close()

        await query.answer("↩️ Отменено!")
        summary = format_session_summary(stats)
        keyboard = build_action_grid(player_id, stats)
        await query.edit_message_text(
            f"📊 **{name}** ({rating:.1f}/10)\n\n"
            f"{summary}\n\n"
            "Маркирайте действие  _(ред = действие · колона = +/=/–)_:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return

    conn.close()


async def toggle_player_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "done_selecting":
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM current_game')
        count = c.fetchone()[0]
        conn.close()
        await query.edit_message_text(
            f"✅ **{count} играчи** маркирани за тази неделя.\n\n"
            "Използвайте /whosplaying или /maketeams!",
            parse_mode='Markdown'
        )
        return

    player_id = int(query.data.split("_")[1])
    conn = get_db_connection()
    c = conn.cursor()

    c.execute('SELECT id FROM current_game WHERE player_id = ?', (player_id,))
    if c.fetchone():
        c.execute('DELETE FROM current_game WHERE player_id = ?', (player_id,))
    else:
        c.execute('INSERT INTO current_game (player_id) VALUES (?)', (player_id,))
    conn.commit()

    c.execute('SELECT id, name, rating FROM players ORDER BY name')
    players = c.fetchall()
    c.execute('SELECT player_id FROM current_game')
    selected = {row[0] for row in c.fetchall()}
    conn.close()

    keyboard = []
    for pid, name, rating in players:
        box = "✅" if pid in selected else "☐"
        keyboard.append([InlineKeyboardButton(
            f"{box} {name} ({rating:.1f})", callback_data=f"toggle_{pid}"
        )])
    keyboard.append([InlineKeyboardButton("✔️ Готово", callback_data="done_selecting")])
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data

    if data.startswith("toggle_") or data == "done_selecting":
        await toggle_player_callback(update, context)
    elif (data.startswith("track_") or data.startswith("score_") or data == "undo_last"):
        await track_callback(update, context)


async def setup_commands(application):
    commands = [
        BotCommand("start",             "Старт на бота"),
        BotCommand("help",              "Помощ и команди"),
        BotCommand("addplayer",         "Добави играч/играчи"),
        BotCommand("players",           "Всички играчи и рейтинги"),
        BotCommand("stats",             "Статистика на играч"),
        BotCommand("deleteplayer",      "Изтрий играч"),
        BotCommand("setratingmanual",   "Ръчна корекция на рейтинг"),
        BotCommand("playing",           "Избери кой играе"),
        BotCommand("whosplaying",       "Виж маркираните"),
        BotCommand("maketeams",         "Генерирай 3 отбора"),
        BotCommand("cleargame",         "Изчисти за нова седмица"),
        BotCommand("startgame",         "Започни следене"),
        BotCommand("track",             "Маркирай действия LIVE"),
        BotCommand("endgame",           "Приключи и обнови рейтинги"),
        BotCommand("history",           "Последните 4 седмици"),
    ]
    await application.bot.set_my_commands(commands)
    print("✅ Командното меню е настроено.")


def main():
    if not BOT_TOKEN:
        raise SystemExit('Set the VOLLEYBALL_BOT_TOKEN environment variable (token from @BotFather).')
    init_db()

    logger.info("=" * 50)
    logger.info("🏐 Волейбол Мениджър Pro v4")
    logger.info(f"📁 Database: {DB_PATH}")
    logger.info("=" * 50)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",           start))
    app.add_handler(CommandHandler("help",            help_command))
    app.add_handler(CommandHandler("addplayer",       add_player))
    app.add_handler(CommandHandler("players",         list_players))
    app.add_handler(CommandHandler("deleteplayer",    delete_player))
    app.add_handler(CommandHandler("setratingmanual", set_rating_manual))
    app.add_handler(CommandHandler("stats",           player_stats))
    app.add_handler(CommandHandler("playing",         select_playing))
    app.add_handler(CommandHandler("whosplaying",     whos_playing))
    app.add_handler(CommandHandler("maketeams",       make_teams))
    app.add_handler(CommandHandler("cleargame",       clear_game))
    app.add_handler(CommandHandler("history",         team_history))
    app.add_handler(CommandHandler("startgame",       start_game))
    app.add_handler(CommandHandler("track",           track_performance))
    app.add_handler(CommandHandler("endgame",         end_game))

    app.add_handler(CallbackQueryHandler(callback_router))
    app.post_init = setup_commands

    print("🏐 Волейбол Мениджър Pro v4 работи...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()