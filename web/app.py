"""
Volleyball Manager — Flask Backend
app.py  (File 1 of 3)

Run:
    pip install flask
    python app.py

The SQLite database is created automatically next to this file.
Templates are loaded from ./templates/  (setup.html, game.html)

API surface
-----------
GET  /                          → setup.html
GET  /game                      → game.html

Players
  GET  /api/players             → list all players
  POST /api/players             → add one or many (comma-separated name)
  PUT  /api/players/<id>        → rename or set manual rating
  DEL  /api/players/<id>        → delete

Session lifecycle
  POST /api/session/start       → create new game-day session
  GET  /api/session/state       → full state snapshot
  POST /api/session/teams       → save manual team assignment
  POST /api/session/auto-teams  → auto-balance 3 teams by rating
  POST /api/session/positions   → save starting positions per team

Live game
  GET  /api/game/court          → current court (teams, positions, score, who serves)
  POST /api/game/log            → log a skill action  {player_id, skill, direction}
  POST /api/game/point          → award a point       {winning_team_slot}
  POST /api/game/undo-point     → reverse last point
  POST /api/game/end-match      → close current match {winner_slot}
  POST /api/game/end-day        → close session, save ratings
  GET  /api/game/summary        → per-player stats for current session
"""

import json
import os
import sqlite3
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple

from flask import Flask, g, jsonify, redirect, render_template, request

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "volleyball.db")

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))
app.config["JSON_ENSURE_ASCII"] = False   # keep Cyrillic intact in responses


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    """Return a per-request SQLite connection (stored on Flask's g object)."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        g.db.row_factory = sqlite3.Row          # rows behave like dicts
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")  # safe for concurrent reads
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create all tables if they don't exist yet."""
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA foreign_keys = ON")
    c = db.cursor()

    # ------------------------------------------------------------------
    # Players — persistent across all sessions
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS players (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    UNIQUE NOT NULL,
            rating          REAL    NOT NULL DEFAULT 5.0,
            serve_r         REAL    NOT NULL DEFAULT 5.0,
            attack_r        REAL    NOT NULL DEFAULT 5.0,
            block_r         REAL    NOT NULL DEFAULT 5.0,
            reception_r     REAL    NOT NULL DEFAULT 5.0,
            defense_r       REAL    NOT NULL DEFAULT 5.0,
            set_r           REAL    NOT NULL DEFAULT 5.0,
            games_played    INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT    DEFAULT (date('now'))
        )
    """)

    # ------------------------------------------------------------------
    # Rating history — one row per end-of-day update per player
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS rating_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id   INTEGER NOT NULL REFERENCES players(id),
            old_rating  REAL    NOT NULL,
            new_rating  REAL    NOT NULL,
            session_date TEXT   NOT NULL,
            changed_at  TEXT    DEFAULT (datetime('now'))
        )
    """)

    # ------------------------------------------------------------------
    # Game sessions — one row per game day
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS game_sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_date    TEXT    NOT NULL DEFAULT (date('now')),
            status          TEXT    NOT NULL DEFAULT 'setup',
            -- status: setup | active | ended
            team_queue_json TEXT,
            -- JSON: {"slots": [1,2,3], "sitting": 3, "consecutive": {1:0,2:0,3:0}}
            -- slots[0] = team A on court, slots[1] = team B on court, slots[2] = sitting
            created_at      TEXT    DEFAULT (datetime('now'))
        )
    """)

    # ------------------------------------------------------------------
    # Teams within a session — one row per team slot (1, 2, 3)
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS session_teams (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      INTEGER NOT NULL REFERENCES game_sessions(id),
            team_slot       INTEGER NOT NULL,   -- 1, 2, or 3
            player_ids_json TEXT    NOT NULL,   -- JSON array of player ids
            positions_json  TEXT,
            -- JSON: {"1": player_id, "2": player_id, ..., "6": player_id}
            -- position numbers follow standard volleyball (1=server/back-right, ...)
            rotation_offset INTEGER NOT NULL DEFAULT 0,
            -- how many times this team has rotated; server = player at (offset % 6)
            UNIQUE(session_id, team_slot)
        )
    """)

    # ------------------------------------------------------------------
    # Matches — one row per individual match (2 teams playing)
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      INTEGER NOT NULL REFERENCES game_sessions(id),
            match_number    INTEGER NOT NULL,
            team_a_slot     INTEGER NOT NULL,
            team_b_slot     INTEGER NOT NULL,
            score_a         INTEGER NOT NULL DEFAULT 0,
            score_b         INTEGER NOT NULL DEFAULT 0,
            winner_slot     INTEGER,            -- NULL while in progress
            started_at      TEXT DEFAULT (datetime('now')),
            ended_at        TEXT
        )
    """)

    # ------------------------------------------------------------------
    # Point log — every single point in every match, enables full undo
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS point_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id        INTEGER NOT NULL REFERENCES matches(id),
            winning_slot    INTEGER NOT NULL,   -- which team_slot won the point
            score_a_after   INTEGER NOT NULL,
            score_b_after   INTEGER NOT NULL,
            rotation_advanced INTEGER NOT NULL DEFAULT 0,
            -- 1 if this point triggered a rotation for the winning team
            prev_server_slot INTEGER,
            -- which slot was serving before this point (to restore on undo)
            logged_at       TEXT    DEFAULT (datetime('now'))
        )
    """)

    # ------------------------------------------------------------------
    # Performance log — skill actions during a match
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS performance_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER NOT NULL REFERENCES game_sessions(id),
            match_id    INTEGER NOT NULL REFERENCES matches(id),
            player_id   INTEGER NOT NULL REFERENCES players(id),
            skill       TEXT    NOT NULL,
            -- serve | attack | block | reception | defense | set
            direction   TEXT    NOT NULL,
            -- plus | minus
            logged_at   TEXT    DEFAULT (datetime('now'))
        )
    """)

    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Rating calculations
# ---------------------------------------------------------------------------

SKILL_WEIGHTS = {
    "serve":     0.15,
    "attack":    0.30,
    "block":     0.15,
    "reception": 0.20,
    "defense":   0.10,
    "set":       0.10,
}

# Maps DB column suffix to weight key
SKILL_COL = {
    "serve":     "serve_r",
    "attack":    "attack_r",
    "block":     "block_r",
    "reception": "reception_r",
    "defense":   "defense_r",
    "set":       "set_r",
}


def calc_skill_score(plus: int, minus: int) -> float:
    """
    Score a skill on 0–10.
    Uses a simplified +/- model (no neutral tracking in this version —
    every action is either good or bad, matching the UI buttons).
    Formula: score = (plus / (plus + minus)) * 10, floored at 0.
    Falls back to 5.0 if no data.
    """
    total = plus + minus
    if total == 0:
        return 5.0
    return round((plus / total) * 10, 2)


def calc_overall_rating(skill_ratings: Dict[str, float]) -> float:
    """Weighted average of all skill scores → 0–10."""
    return round(
        sum(skill_ratings.get(s, 5.0) * w for s, w in SKILL_WEIGHTS.items()), 2
    )


def adaptive_weight(total_actions: int) -> float:
    """
    How much to trust this session's data vs historical rating.
    Fewer actions → smaller update → rating stays more stable.
    """
    if total_actions < 5:   return 0.0   # not enough — skip
    if total_actions < 10:  return 0.20
    if total_actions < 20:  return 0.30
    if total_actions < 35:  return 0.40
    return 0.50


def smooth(new_val: float, old_val: float, weight: float) -> float:
    return round(new_val * weight + old_val * (1.0 - weight), 2)


# ---------------------------------------------------------------------------
# Team balancing
# ---------------------------------------------------------------------------

def auto_balance_teams(
    players: List[Dict],
) -> Tuple[List[int], List[int], List[int]]:
    """
    Greedy snake-draft: sort by rating descending, assign each player
    to the weakest team.  Returns three lists of player IDs.
    Raises ValueError if len(players) != 18.
    """
    if len(players) != 18:
        raise ValueError(f"Need exactly 18 players, got {len(players)}")

    sorted_p = sorted(players, key=lambda p: p["rating"], reverse=True)
    teams: List[List[int]] = [[], [], []]
    strengths = [0.0, 0.0, 0.0]

    for p in sorted_p:
        idx = strengths.index(min(strengths))
        teams[idx].append(p["id"])
        strengths[idx] += p["rating"]

    return teams[0], teams[1], teams[2]


# ---------------------------------------------------------------------------
# Game queue logic
# ---------------------------------------------------------------------------

def initial_queue(team_slots: List[int]) -> Dict:
    """
    Returns the initial queue state.
    slots[0] and slots[1] play first; slots[2] sits.
    consecutive tracks how many matches in a row each slot has played.
    serving_slot: which team_slot is currently serving (starts as team_a).
    """
    return {
        "playing":     [team_slots[0], team_slots[1]],  # [team_a, team_b]
        "sitting":     team_slots[2],
        "consecutive": {str(s): 0 for s in team_slots},
        "serving_slot": team_slots[0],   # team_a serves first by default
    }


def next_queue(queue: Dict, winner_slot: int, loser_slot: int) -> Dict:
    """
    Compute the next queue after a match result.

    Rules:
    1. Winner stays on court, plays the sitting team.
    2. Loser sits out.
    3. If the same team has now won 2 in a row → the two losers play each other
       next (the long-winning team gets a rest).
    4. No team plays more than 2 matches in a row.

    Returns a new queue dict.
    """
    new_queue = json.loads(json.dumps(queue))   # deep copy

    # Update consecutive counters
    new_queue["consecutive"][str(winner_slot)] = \
        queue["consecutive"].get(str(winner_slot), 0) + 1
    new_queue["consecutive"][str(loser_slot)] = 0
    sitting = queue["sitting"]
    # sitting team's counter stays unchanged (they didn't play)

    winner_consec = new_queue["consecutive"][str(winner_slot)]

    if winner_consec >= 2:
        # Winner has played 2 in a row → force them to sit
        # The two teams that haven't played 2 in a row go on court
        # sitting team plays against the loser
        new_queue["playing"]     = [sitting, loser_slot]
        new_queue["sitting"]     = winner_slot
        new_queue["consecutive"][str(winner_slot)] = 0
        new_queue["serving_slot"] = sitting
    else:
        # Normal: winner stays, sitting team comes on, loser sits
        new_queue["playing"]     = [winner_slot, sitting]
        new_queue["sitting"]     = loser_slot
        new_queue["serving_slot"] = winner_slot   # winner serves next match

    return new_queue


# ---------------------------------------------------------------------------
# Position helpers
# ---------------------------------------------------------------------------

# Standard volleyball positions (1-indexed):
#   1 = back-right  (server)
#   2 = front-right
#   3 = front-middle
#   4 = front-left
#   5 = back-left
#   6 = back-middle

POSITION_SKILLS = {
    1: ["serve", "reception", "set"],           # server / back-right
    2: ["attack", "set", "block"],              # front-right
    3: ["set", "block", "attack"],              # front-middle (setter row)
    4: ["attack", "set", "block"],              # front-left
    5: ["reception", "set"],                    # back-left
    6: ["reception", "set", "attack"],          # back-middle / libero area
}

# Bulgarian display labels for each skill
SKILL_LABELS = {
    "serve":     "Сервис",
    "attack":    "Атака",
    "block":     "Блок",
    "reception": "Посрещане",
    "defense":   "Пас",
    "set":       "Вдигане",
}


def player_position(team_row: sqlite3.Row, player_id: int) -> int:
    """
    Given a session_teams row, return the CURRENT position (1-6) of a player,
    accounting for rotation offset.
    positions_json is {position: player_id}, so we invert it first.
    """
    if not team_row["positions_json"]:
        return 1
    pos_map = json.loads(team_row["positions_json"])      # {"1": pid, ...}
    pid_to_pos = {int(v): int(k) for k, v in pos_map.items()}
    base_pos = pid_to_pos.get(player_id, 1)               # starting position
    offset   = team_row["rotation_offset"] % 6
    # Volleyball rotation: winning a point → everyone moves one position clockwise
    # Position numbering goes 1→6→5→4→3→2→1 for "clockwise" rotation.
    ROTATION_ORDER = [1, 2, 3, 4, 5, 6]  # positions in clockwise order
    idx = ROTATION_ORDER.index(base_pos)
    current_idx = (idx - offset) % 6     # rotate backwards = move clockwise
    return ROTATION_ORDER[current_idx]


def current_server_id(team_row: sqlite3.Row) -> Optional[int]:
    """Return the player_id of the current server (position 1 after rotation)."""
    if not team_row["positions_json"]:
        return None
    pos_map = json.loads(team_row["positions_json"])   # {pos_str: player_id}
    offset  = team_row["rotation_offset"] % 6
    ROTATION_ORDER = [1, 2, 3, 4, 5, 6]
    # After `offset` rotations, position 1 is held by the player who started
    # at position (1 + offset) % 6  (1-indexed so we map through the list)
    server_base_pos = ROTATION_ORDER[offset % 6]
    return pos_map.get(str(server_base_pos))


# ---------------------------------------------------------------------------
# State snapshot builder  (used by /api/session/state and /api/game/court)
# ---------------------------------------------------------------------------

def build_state_snapshot(session_id: int) -> Dict:
    """
    Build the full state dict returned by both /api/session/state
    and /api/game/court.  Keeps both routes in sync automatically.
    """
    db = get_db()

    # Session
    session = db.execute(
        "SELECT * FROM game_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if not session:
        return {}

    queue = json.loads(session["team_queue_json"]) if session["team_queue_json"] else {}

    # Active match (if any)
    match = db.execute(
        """SELECT * FROM matches
           WHERE session_id = ? AND winner_slot IS NULL
           ORDER BY id DESC LIMIT 1""",
        (session_id,),
    ).fetchone()

    # Teams
    teams_out = {}
    team_rows = db.execute(
        "SELECT * FROM session_teams WHERE session_id = ? ORDER BY team_slot",
        (session_id,),
    ).fetchall()

    all_player_ids = []
    for row in team_rows:
        ids = json.loads(row["player_ids_json"])
        all_player_ids.extend(ids)

    # Fetch all relevant players in one query
    if all_player_ids:
        ph = ",".join("?" * len(all_player_ids))
        players_q = db.execute(
            f"SELECT * FROM players WHERE id IN ({ph})", all_player_ids
        ).fetchall()
        player_map = {p["id"]: dict(p) for p in players_q}
    else:
        player_map = {}

    # Build first-name frequency map across ALL players in this session so we
    # can disambiguate duplicates with a last-name initial  ("Иван П.").
    first_name_freq: Dict[str, int] = {}
    for p in player_map.values():
        fname = (p.get("name") or "").split()[0]
        first_name_freq[fname] = first_name_freq.get(fname, 0) + 1

    for row in team_rows:
        slot = row["team_slot"]
        ids  = json.loads(row["player_ids_json"])
        pos_map = json.loads(row["positions_json"]) if row["positions_json"] else {}

        players_list = []
        for pid in ids:
            p       = player_map.get(pid, {})
            cur_pos = player_position(row, pid) if pos_map else None
            server  = current_server_id(row)

            # Display name: "Иван" normally, "Иван П." when first name clashes
            name_parts   = (p.get("name") or "").split()
            fname        = name_parts[0] if name_parts else "?"
            if first_name_freq.get(fname, 1) > 1 and len(name_parts) > 1:
                display_name = fname + "\u00a0" + name_parts[1][0] + "."
            else:
                display_name = fname

            players_list.append({
                "id":           pid,
                "name":         p.get("name", ""),
                "first_name":   display_name,
                "rating":       p.get("rating", 5.0),
                "position":     cur_pos,
                "is_server":    (pid == server) if server else False,
                "skills":       POSITION_SKILLS.get(cur_pos, []) if cur_pos else [],
                "skill_labels": {
                    s: SKILL_LABELS[s]
                    for s in POSITION_SKILLS.get(cur_pos, [])
                    if s in SKILL_LABELS
                },
            })

        teams_out[str(slot)] = {
            "slot":             slot,
            "players":          players_list,
            "rotation_offset":  row["rotation_offset"],
            "strength":         round(sum(player_map.get(pid, {}).get("rating", 5.0) for pid in ids), 1),
        }

    # Score from active match
    score = {"a": 0, "b": 0}
    playing = queue.get("playing", [])
    if match:
        score = {"a": match["score_a"], "b": match["score_b"]}

    # Match history
    finished = db.execute(
        """SELECT * FROM matches
           WHERE session_id = ? AND winner_slot IS NOT NULL
           ORDER BY match_number""",
        (session_id,),
    ).fetchall()
    history = [dict(m) for m in finished]

    return {
        "session_id":    session_id,
        "status":        session["status"],
        "session_date":  session["session_date"],
        "queue":         queue,
        "playing":       playing,                 # [slot_a, slot_b] on court
        "sitting":       queue.get("sitting"),
        "serving_slot":  queue.get("serving_slot"),
        "teams":         teams_out,
        "score":         score,
        "match_number":  (match["match_number"] if match else None),
        "match_id":      (match["id"] if match else None),
        "match_history": history,
    }


# ---------------------------------------------------------------------------
# Utility: get active session id (returns None if no active session)
# ---------------------------------------------------------------------------

def active_session_id() -> Optional[int]:
    db = get_db()
    row = db.execute(
        "SELECT id FROM game_sessions WHERE status != 'ended' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


# ===========================================================================
# Routes — Pages
# ===========================================================================

@app.route("/")
def index():
    return render_template("setup.html")


@app.route("/game")
def game():
    return render_template("game.html")


# ===========================================================================
# Routes — Players
# ===========================================================================

@app.route("/api/players", methods=["GET"])
def api_players_list():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM players ORDER BY rating DESC, name"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/players", methods=["POST"])
def api_players_add():
    """
    Body: {"name": "Иван"} or {"name": "Иван, Мария, Петър"}
    Returns list of created/skipped players.
    """
    data  = request.get_json(force=True)
    names = [n.strip() for n in data.get("name", "").split(",") if n.strip()]
    if not names:
        return jsonify({"error": "Не е зададено име"}), 400

    db = get_db()
    added, skipped = [], []
    for name in names:
        try:
            cur = db.execute(
                "INSERT INTO players (name) VALUES (?)", (name,)
            )
            added.append({"id": cur.lastrowid, "name": name, "rating": 5.0})
        except sqlite3.IntegrityError:
            skipped.append(name)
    db.commit()
    return jsonify({"added": added, "skipped": skipped})


@app.route("/api/players/<int:player_id>", methods=["PUT"])
def api_players_update(player_id):
    """
    Body may contain: {"name": "...", "rating": 7.5}
    Only provided fields are updated.
    """
    db   = get_db()
    data = request.get_json(force=True)
    row  = db.execute("SELECT * FROM players WHERE id=?", (player_id,)).fetchone()
    if not row:
        return jsonify({"error": "Играч не е намерен"}), 404

    new_name   = data.get("name",   row["name"])
    new_rating = float(data.get("rating", row["rating"]))
    if not (1.0 <= new_rating <= 10.0):
        return jsonify({"error": "Рейтингът трябва да е между 1 и 10"}), 400

    if new_name != row["name"]:
        try:
            db.execute("UPDATE players SET name=? WHERE id=?", (new_name, player_id))
        except sqlite3.IntegrityError:
            return jsonify({"error": f"'{new_name}' вече съществува"}), 409

    if new_rating != row["rating"]:
        db.execute(
            """INSERT INTO rating_history
               (player_id, old_rating, new_rating, session_date)
               VALUES (?,?,?,?)""",
            (player_id, row["rating"], new_rating, str(date.today())),
        )
        # When manually setting rating, also reset all sub-skills to same value
        db.execute(
            """UPDATE players SET
               name=?, rating=?, serve_r=?, attack_r=?, block_r=?,
               reception_r=?, defense_r=?, set_r=?
               WHERE id=?""",
            (new_name, new_rating, new_rating, new_rating, new_rating,
             new_rating, new_rating, player_id),
        )
    else:
        db.execute("UPDATE players SET name=? WHERE id=?", (new_name, player_id))

    db.commit()
    updated = db.execute("SELECT * FROM players WHERE id=?", (player_id,)).fetchone()
    return jsonify(dict(updated))


@app.route("/api/players/<int:player_id>", methods=["DELETE"])
def api_players_delete(player_id):
    db = get_db()
    db.execute("DELETE FROM players WHERE id=?", (player_id,))
    db.commit()
    return jsonify({"ok": True})


# ===========================================================================
# Routes — Session setup
# ===========================================================================

@app.route("/api/session/start", methods=["POST"])
def api_session_start():
    """
    Create a new game-day session.
    Any previous non-ended session is left intact (call end-day to close it).
    """
    db = get_db()
    cur = db.execute(
        "INSERT INTO game_sessions (status) VALUES ('setup')"
    )
    db.commit()
    return jsonify({"session_id": cur.lastrowid})


@app.route("/api/session/state", methods=["GET"])
def api_session_state():
    sid = active_session_id()
    if sid is None:
        return jsonify({"status": "no_session"})
    return jsonify(build_state_snapshot(sid))


@app.route("/api/session/teams", methods=["POST"])
def api_session_teams():
    """
    Save manual team assignment.
    Body: {"session_id": 1, "team1": [id,...], "team2": [id,...], "team3": [id,...]}
    Each team must have exactly 6 players.  All 18 must be distinct.
    """
    db   = get_db()
    data = request.get_json(force=True)
    sid  = data.get("session_id") or active_session_id()
    if not sid:
        return jsonify({"error": "Няма активна сесия"}), 400

    teams = {
        1: [int(i) for i in data.get("team1", [])],
        2: [int(i) for i in data.get("team2", [])],
        3: [int(i) for i in data.get("team3", [])],
    }

    for slot, ids in teams.items():
        if len(ids) != 6:
            return jsonify({"error": f"Отбор {slot} трябва да има точно 6 играча"}), 400
    all_ids = teams[1] + teams[2] + teams[3]
    if len(set(all_ids)) != 18:
        return jsonify({"error": "Играчите трябва да са 18 различни"}), 400

    for slot, ids in teams.items():
        db.execute(
            """INSERT INTO session_teams (session_id, team_slot, player_ids_json)
               VALUES (?,?,?)
               ON CONFLICT(session_id, team_slot) DO UPDATE
               SET player_ids_json=excluded.player_ids_json,
                   positions_json=NULL,
                   rotation_offset=0""",
            (sid, slot, json.dumps(ids)),
        )
    db.commit()
    return jsonify({"ok": True, "session_id": sid})


@app.route("/api/session/auto-teams", methods=["POST"])
def api_session_auto_teams():
    """
    Auto-balance 3 teams from the 18 selected players.
    Body: {"session_id": 1} or uses active session.
    """
    db   = get_db()
    data = request.get_json(force=True) or {}
    sid  = data.get("session_id") or active_session_id()
    if not sid:
        return jsonify({"error": "Няма активна сесия"}), 400

    player_ids = data.get("player_ids")
    if player_ids:
        # Use specific players
        ph = ",".join("?" * len(player_ids))
        players = db.execute(
            f"SELECT id, name, rating FROM players WHERE id IN ({ph})", player_ids
        ).fetchall()
        players = [dict(p) for p in players]
        if len(players) != 18:
            return jsonify({"error": f"Нужни са 18 играча, намерени: {len(players)}"}), 400
    else:
        # Fallback: top 18 by rating (existing behavior)
        players = db.execute(
            "SELECT id, name, rating FROM players ORDER BY rating DESC"
        ).fetchall()
        players = [dict(p) for p in players]
        if len(players) < 18:
            return jsonify({"error": f"Нужни са 18 играча, налични: {len(players)}"}), 400
        players = players[:18]

    try:
        t1, t2, t3 = auto_balance_teams(players)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    teams = {1: t1, 2: t2, 3: t3}
    for slot, ids in teams.items():
        db.execute(
            """INSERT INTO session_teams (session_id, team_slot, player_ids_json)
               VALUES (?,?,?)
               ON CONFLICT(session_id, team_slot) DO UPDATE
               SET player_ids_json=excluded.player_ids_json,
                   positions_json=NULL,
                   rotation_offset=0""",
            (sid, slot, json.dumps(ids)),
        )
    db.commit()

    # Return the balanced teams with player names
    pid_map = {p["id"]: p for p in players}
    result  = {}
    for slot, ids in teams.items():
        strength = round(sum(pid_map[i]["rating"] for i in ids), 1)
        result[str(slot)] = {
            "slot":     slot,
            "player_ids": ids,
            "players":  [{"id": i, "name": pid_map[i]["name"], "rating": pid_map[i]["rating"]} for i in ids],
            "strength": strength,
        }
    return jsonify({"ok": True, "session_id": sid, "teams": result})


@app.route("/api/session/positions", methods=["POST"])
def api_session_positions():
    """
    Save starting positions for one or all teams, then transition session to 'active'
    and create the first match.

    Body:
    {
        "session_id": 1,
        "positions": {
            "1": {"1": pid, "2": pid, "3": pid, "4": pid, "5": pid, "6": pid},
            "2": { ... },
            "3": { ... }
        },
        "serving_slot": 1      ← which team serves first (optional, default 1)
    }
    """
    db   = get_db()
    data = request.get_json(force=True)
    sid  = data.get("session_id") or active_session_id()
    if not sid:
        return jsonify({"error": "Няма активна сесия"}), 400

    positions = data.get("positions", {})
    for slot_str, pos_map in positions.items():
        slot = int(slot_str)
        if len(pos_map) != 6:
            return jsonify({"error": f"Отбор {slot} трябва да има 6 позиции"}), 400
        db.execute(
            """UPDATE session_teams SET positions_json=?
               WHERE session_id=? AND team_slot=?""",
            (json.dumps({str(k): int(v) for k, v in pos_map.items()}), sid, slot),
        )

    # Verify all 3 teams have positions before activating
    rows = db.execute(
        "SELECT team_slot, positions_json FROM session_teams WHERE session_id=?", (sid,)
    ).fetchall()
    all_set = all(r["positions_json"] for r in rows)

    if all_set:
        serving_slot = int(data.get("serving_slot", 1))
        queue = initial_queue([1, 2, 3])
        queue["serving_slot"] = serving_slot

        db.execute(
            "UPDATE game_sessions SET status='active', team_queue_json=? WHERE id=?",
            (json.dumps(queue), sid),
        )
        # Create first match
        db.execute(
            """INSERT INTO matches (session_id, match_number, team_a_slot, team_b_slot)
               VALUES (?,1,?,?)""",
            (sid, queue["playing"][0], queue["playing"][1]),
        )

    db.commit()
    return jsonify({"ok": True, "activated": all_set})


# ===========================================================================
# Routes — Live game
# ===========================================================================

@app.route("/api/game/court", methods=["GET"])
def api_game_court():
    sid = active_session_id()
    if sid is None:
        return jsonify({"error": "Няма активна сесия"}), 404
    return jsonify(build_state_snapshot(sid))


@app.route("/api/game/log", methods=["POST"])
def api_game_log():
    """
    Log a skill action (tap on +/- button in player popup).
    Body: {"player_id": 5, "skill": "attack", "direction": "plus"}
    Accepts "plus"/"minus" or "+"/"-".
    """
    db   = get_db()
    data = request.get_json(force=True)
    sid  = active_session_id()
    if not sid:
        return jsonify({"error": "Няма активна сесия"}), 400

    player_id = int(data["player_id"])
    skill     = data["skill"]
    direction = data.get("direction", "plus")
    if direction == "+":
        direction = "plus"
    elif direction == "-":
        direction = "minus"

    if skill not in SKILL_WEIGHTS:
        return jsonify({"error": f"Непознато умение: {skill}"}), 400
    if direction not in ("plus", "minus"):
        return jsonify({"error": "Direction трябва да е 'plus' или 'minus'"}), 400

    match = db.execute(
        """SELECT id FROM matches
           WHERE session_id=? AND winner_slot IS NULL
           ORDER BY id DESC LIMIT 1""",
        (sid,),
    ).fetchone()
    if not match:
        return jsonify({"error": "Няма активен мач"}), 400

    db.execute(
        """INSERT INTO performance_log
           (session_id, match_id, player_id, skill, direction)
           VALUES (?,?,?,?,?)""",
        (sid, match["id"], player_id, skill, direction),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/game/point", methods=["POST"])
def api_game_point():
    """
    Award a point to one team.
    Body: {"winning_team_slot": 1}

    Rules:
    - Score increments for the winning team.
    - If winning_team != currently_serving → side-out → rotate winning team.
    - No rotation on the very first point of a match (handled by client
      NOT calling this for set-up; but we also track match point count).
    - Returns updated court state.
    """
    db   = get_db()
    data = request.get_json(force=True)
    sid  = active_session_id()
    if not sid:
        return jsonify({"error": "Няма активна сесия"}), 400

    winning_slot = int(data["winning_team_slot"])

    session = db.execute(
        "SELECT * FROM game_sessions WHERE id=?", (sid,)
    ).fetchone()
    queue = json.loads(session["team_queue_json"])

    match = db.execute(
        """SELECT * FROM matches
           WHERE session_id=? AND winner_slot IS NULL
           ORDER BY id DESC LIMIT 1""",
        (sid,),
    ).fetchone()
    if not match:
        return jsonify({"error": "Няма активен мач"}), 400

    # Determine team_a / team_b slots for this match
    team_a_slot = match["team_a_slot"]
    team_b_slot = match["team_b_slot"]

    if winning_slot not in (team_a_slot, team_b_slot):
        return jsonify({"error": "winning_team_slot не е в текущия мач"}), 400

    # Current score
    score_a = match["score_a"]
    score_b = match["score_b"]

    # Is this a side-out?  (winning team was NOT serving)
    prev_serving = queue.get("serving_slot", team_a_slot)
    is_sideout   = (winning_slot != prev_serving)

    # No-rotation-on-first-point rule (per team individually):
    # The very first point a team wins in this match never triggers a rotation,
    # even if it is a side-out. After that, normal volleyball rotation applies.
    prev_wins_for_team = db.execute(
        "SELECT COUNT(*) FROM point_log WHERE match_id=? AND winning_slot=?",
        (match["id"], winning_slot),
    ).fetchone()[0]
    is_first_point_for_team = (prev_wins_for_team == 0)

    # Increment score
    if winning_slot == team_a_slot:
        score_a += 1
    else:
        score_b += 1

    db.execute(
        "UPDATE matches SET score_a=?, score_b=? WHERE id=?",
        (score_a, score_b, match["id"]),
    )

    # Serve always transfers on side-out; rotation only on side-out + not first point
    rotation_advanced = 0
    if is_sideout:
        queue["serving_slot"] = winning_slot
        if not is_first_point_for_team:
            db.execute(
                """UPDATE session_teams
                   SET rotation_offset = rotation_offset + 1
                   WHERE session_id=? AND team_slot=?""",
                (sid, winning_slot),
            )
            rotation_advanced = 1

    # Log point
    db.execute(
        """INSERT INTO point_log
           (match_id, winning_slot, score_a_after, score_b_after,
            rotation_advanced, prev_server_slot)
           VALUES (?,?,?,?,?,?)""",
        (match["id"], winning_slot, score_a, score_b,
         rotation_advanced, prev_serving),
    )

    db.execute(
        "UPDATE game_sessions SET team_queue_json=? WHERE id=?",
        (json.dumps(queue), sid),
    )
    db.commit()

    return jsonify(build_state_snapshot(sid))


@app.route("/api/game/undo-point", methods=["POST"])
def api_game_undo_point():
    """
    Reverse the last point: restore score, undo rotation if it was advanced.
    """
    db  = get_db()
    sid = active_session_id()
    if not sid:
        return jsonify({"error": "Няма активна сесия"}), 400

    match = db.execute(
        """SELECT * FROM matches
           WHERE session_id=? AND winner_slot IS NULL
           ORDER BY id DESC LIMIT 1""",
        (sid,),
    ).fetchone()
    if not match:
        return jsonify({"error": "Няма активен мач"}), 400

    last_point = db.execute(
        """SELECT * FROM point_log
           WHERE match_id=? ORDER BY id DESC LIMIT 1""",
        (match["id"],),
    ).fetchone()
    if not last_point:
        return jsonify({"error": "Няма точки за отмяна"}), 400

    # Restore score
    prev_a = last_point["score_a_after"] - (1 if last_point["winning_slot"] == match["team_a_slot"] else 0)
    prev_b = last_point["score_b_after"] - (1 if last_point["winning_slot"] == match["team_b_slot"] else 0)
    db.execute(
        "UPDATE matches SET score_a=?, score_b=? WHERE id=?",
        (prev_a, prev_b, match["id"]),
    )

    # Undo rotation
    if last_point["rotation_advanced"]:
        db.execute(
            """UPDATE session_teams
               SET rotation_offset = MAX(0, rotation_offset - 1)
               WHERE session_id=? AND team_slot=?""",
            (sid, last_point["winning_slot"]),
        )
        # Restore previous server
        session = db.execute(
            "SELECT team_queue_json FROM game_sessions WHERE id=?", (sid,)
        ).fetchone()
        queue = json.loads(session["team_queue_json"])
        queue["serving_slot"] = last_point["prev_server_slot"]
        db.execute(
            "UPDATE game_sessions SET team_queue_json=? WHERE id=?",
            (json.dumps(queue), sid),
        )

    # Delete the point log entry
    db.execute("DELETE FROM point_log WHERE id=?", (last_point["id"],))
    db.commit()
    return jsonify(build_state_snapshot(sid))


@app.route("/api/game/positions", methods=["PUT"])
def api_game_update_positions():
    """
    Reassign starting positions for one team mid-game.
    Resets that team's rotation_offset to 0 (new base positions = fresh start).

    Body: {
        "slot": 1,
        "positions": {"1": player_id, "2": player_id, ..., "6": player_id}
    }
    """
    db   = get_db()
    data = request.get_json(force=True)
    sid  = active_session_id()
    if not sid:
        return jsonify({"error": "Няма активна сесия"}), 400

    slot = int(data.get("slot", 0))
    if slot not in (1, 2, 3):
        return jsonify({"error": "slot трябва да е 1, 2 или 3"}), 400

    pos_map = data.get("positions", {})
    if len(pos_map) != 6:
        return jsonify({"error": "Нужни са точно 6 позиции"}), 400

    # Validate all 6 position keys (1-6) and that players belong to this team
    team_row = db.execute(
        "SELECT player_ids_json FROM session_teams WHERE session_id=? AND team_slot=?",
        (sid, slot),
    ).fetchone()
    if not team_row:
        return jsonify({"error": f"Отбор {slot} не е намерен"}), 404

    team_ids = set(json.loads(team_row["player_ids_json"]))
    clean_map = {}
    for pos_str, pid in pos_map.items():
        pos = int(pos_str)
        pid = int(pid)
        if pos not in range(1, 7):
            return jsonify({"error": f"Невалидна позиция: {pos}"}), 400
        if pid not in team_ids:
            return jsonify({"error": f"Играч {pid} не е в Отбор {slot}"}), 400
        clean_map[str(pos)] = pid

    db.execute(
        """UPDATE session_teams
           SET positions_json=?, rotation_offset=0
           WHERE session_id=? AND team_slot=?""",
        (json.dumps(clean_map), sid, slot),
    )
    db.commit()
    return jsonify(build_state_snapshot(sid))


@app.route("/api/game/substitute", methods=["POST"])
def api_game_substitute():
    """
    Replace a player on a team with a substitute from the full roster.
    Body: {"team_slot": 1, "out_player_id": 5, "in_player_id": 12}

    The substitute takes the outgoing player's position.
    """
    db   = get_db()
    data = request.get_json(force=True)
    sid  = active_session_id()
    if not sid:
        return jsonify({"error": "Няма активна сесия"}), 400

    slot       = int(data.get("team_slot", 0))
    out_pid    = int(data.get("out_player_id", 0))
    in_pid     = int(data.get("in_player_id", 0))

    if slot not in (1, 2, 3):
        return jsonify({"error": "Невалиден отбор"}), 400

    # Get team data
    team_row = db.execute(
        "SELECT * FROM session_teams WHERE session_id=? AND team_slot=?",
        (sid, slot),
    ).fetchone()
    if not team_row:
        return jsonify({"error": f"Отбор {slot} не е намерен"}), 404

    player_ids = json.loads(team_row["player_ids_json"])
    if out_pid not in player_ids:
        return jsonify({"error": "Играчът не е в този отбор"}), 400

    # Check in_player is not already in any team
    all_teams = db.execute(
        "SELECT player_ids_json FROM session_teams WHERE session_id=?", (sid,)
    ).fetchall()
    all_in_play = set()
    for t in all_teams:
        all_in_play.update(json.loads(t["player_ids_json"]))
    if in_pid in all_in_play:
        return jsonify({"error": "Този играч вече е в отбор"}), 400

    # Check substitute exists
    sub = db.execute("SELECT id FROM players WHERE id=?", (in_pid,)).fetchone()
    if not sub:
        return jsonify({"error": "Играчът не е намерен"}), 404

    # Swap in player_ids
    new_ids = [in_pid if pid == out_pid else pid for pid in player_ids]

    # Swap in positions
    pos_json = team_row["positions_json"]
    if pos_json:
        pos_map = json.loads(pos_json)
        new_pos = {k: (in_pid if v == out_pid else v) for k, v in pos_map.items()}
        pos_json = json.dumps(new_pos)

    db.execute(
        """UPDATE session_teams
           SET player_ids_json=?, positions_json=?
           WHERE session_id=? AND team_slot=?""",
        (json.dumps(new_ids), pos_json, sid, slot),
    )
    db.commit()
    return jsonify(build_state_snapshot(sid))


@app.route("/api/game/end-match", methods=["POST"])
def api_game_end_match():
    """
    End the current match.
    Body: {"winner_slot": 2}

    - Marks match as ended with a winner.
    - Advances the team queue.
    - Creates the next match.
    - Returns updated state.
    """
    db   = get_db()
    data = request.get_json(force=True)
    sid  = active_session_id()
    if not sid:
        return jsonify({"error": "Няма активна сесия"}), 400

    winner_slot = int(data["winner_slot"])

    match = db.execute(
        """SELECT * FROM matches
           WHERE session_id=? AND winner_slot IS NULL
           ORDER BY id DESC LIMIT 1""",
        (sid,),
    ).fetchone()
    if not match:
        return jsonify({"error": "Няма активен мач"}), 400

    loser_slot = (
        match["team_b_slot"] if winner_slot == match["team_a_slot"]
        else match["team_a_slot"]
    )

    # Close match
    db.execute(
        "UPDATE matches SET winner_slot=?, ended_at=datetime('now') WHERE id=?",
        (winner_slot, match["id"]),
    )

    # Advance queue
    session = db.execute(
        "SELECT * FROM game_sessions WHERE id=?", (sid,)
    ).fetchone()
    old_queue = json.loads(session["team_queue_json"])
    new_queue = next_queue(old_queue, winner_slot, loser_slot)

    db.execute(
        "UPDATE game_sessions SET team_queue_json=? WHERE id=?",
        (json.dumps(new_queue), sid),
    )

    # Reset rotation of sitting (losing) team? — No, rotations carry across
    # matches for accurate rating. We only reset rotation_offset at start of day.

    # Create next match
    match_number = match["match_number"] + 1
    db.execute(
        """INSERT INTO matches
           (session_id, match_number, team_a_slot, team_b_slot)
           VALUES (?,?,?,?)""",
        (sid, match_number, new_queue["playing"][0], new_queue["playing"][1]),
    )

    db.commit()
    return jsonify(build_state_snapshot(sid))


@app.route("/api/game/end-day", methods=["POST"])
def api_game_end_day():
    """
    Close the session and save ratings.

    Steps:
    1. Aggregate all performance_log rows for each player.
    2. Compute new skill sub-ratings via adaptive smoothing.
    3. Update players table + insert rating_history rows.
    4. Mark session as 'ended'.
    5. Return a rating summary per player.
    """
    db  = get_db()
    sid = active_session_id()
    if not sid:
        return jsonify({"error": "Няма активна сесия"}), 400

    session_date = str(date.today())

    # Aggregate per player per skill
    logs = db.execute(
        """SELECT player_id, skill, direction, COUNT(*) as cnt
           FROM performance_log
           WHERE session_id=?
           GROUP BY player_id, skill, direction""",
        (sid,),
    ).fetchall()

    # Build structure: {player_id: {skill: {plus:N, minus:N}}}
    perf: Dict[int, Dict[str, Dict[str, int]]] = {}
    for row in logs:
        pid   = row["player_id"]
        skill = row["skill"]
        cnt   = row["cnt"]
        perf.setdefault(pid, {})
        perf[pid].setdefault(skill, {"plus": 0, "minus": 0})
        perf[pid][skill][row["direction"]] += cnt

    # Get all involved players
    all_player_ids = list(perf.keys())
    if not all_player_ids:
        db.execute("UPDATE game_sessions SET status='ended' WHERE id=?", (sid,))
        db.commit()
        return jsonify({"ok": True, "updates": [], "message": "Няма маркирани действия"})

    ph      = ",".join("?" * len(all_player_ids))
    players = db.execute(
        f"SELECT * FROM players WHERE id IN ({ph})", all_player_ids
    ).fetchall()

    updates = []
    for player in players:
        pid = player["id"]
        player_perf = perf.get(pid, {})

        # Total actions logged for this player
        total_actions = sum(
            v["plus"] + v["minus"]
            for v in player_perf.values()
        )
        weight = adaptive_weight(total_actions)

        if weight == 0.0:
            updates.append({
                "player_id":   pid,
                "name":        player["name"],
                "old_rating":  player["rating"],
                "new_rating":  player["rating"],
                "skipped":     True,
                "reason":      f"Само {total_actions} действия (нужни ≥5)",
            })
            continue

        # Compute new sub-skill ratings
        new_skills = {}
        for skill, col in SKILL_COL.items():
            counts     = player_perf.get(skill, {})
            plus_cnt   = counts.get("plus",  0)
            minus_cnt  = counts.get("minus", 0)
            if (plus_cnt + minus_cnt) >= 2:
                game_score  = calc_skill_score(plus_cnt, minus_cnt)
                new_skills[skill] = smooth(game_score, player[col], weight)
            else:
                new_skills[skill] = player[col]

        new_overall = calc_overall_rating(new_skills)
        old_rating  = player["rating"]

        db.execute(
            """UPDATE players SET
               rating=?, serve_r=?, attack_r=?, block_r=?,
               reception_r=?, defense_r=?, set_r=?,
               games_played=games_played+1
               WHERE id=?""",
            (
                new_overall,
                new_skills["serve"],   new_skills["attack"],
                new_skills["block"],   new_skills["reception"],
                new_skills["defense"], new_skills["set"],
                pid,
            ),
        )
        db.execute(
            """INSERT INTO rating_history
               (player_id, old_rating, new_rating, session_date)
               VALUES (?,?,?,?)""",
            (pid, old_rating, new_overall, session_date),
        )

        updates.append({
            "player_id":      pid,
            "name":           player["name"],
            "old_rating":     old_rating,
            "new_rating":     new_overall,
            "change":         round(new_overall - old_rating, 2),
            "total_actions":  total_actions,
            "skipped":        False,
            "new_skills":     new_skills,
        })

    db.execute("UPDATE game_sessions SET status='ended' WHERE id=?", (sid,))
    db.commit()

    updates.sort(key=lambda u: abs(u.get("change", 0)), reverse=True)
    return jsonify({"ok": True, "session_date": session_date, "updates": updates})


@app.route("/api/game/summary", methods=["GET"])
def api_game_summary():
    """Per-player action counts for the current session (for live stats display)."""
    db  = get_db()
    sid = active_session_id()
    if not sid:
        return jsonify({"error": "Няма активна сесия"}), 400

    logs = db.execute(
        """SELECT pl.player_id, p.name, pl.skill, pl.direction, COUNT(*) as cnt
           FROM performance_log pl
           JOIN players p ON p.id = pl.player_id
           WHERE pl.session_id=?
           GROUP BY pl.player_id, pl.skill, pl.direction
           ORDER BY p.name, pl.skill""",
        (sid,),
    ).fetchall()

    summary: Dict[int, Dict] = {}
    for row in logs:
        pid = row["player_id"]
        if pid not in summary:
            summary[pid] = {"player_id": pid, "name": row["name"], "skills": {}}
        skill = row["skill"]
        if skill not in summary[pid]["skills"]:
            summary[pid]["skills"][skill] = {"plus": 0, "minus": 0}
        summary[pid]["skills"][skill][row["direction"]] += row["cnt"]

    return jsonify(list(summary.values()))


# ===========================================================================
# Dev helpers  (remove or guard behind auth in production)
# ===========================================================================

@app.route("/api/dev/reset", methods=["POST"])
def api_dev_reset():
    """
    WARNING: Drops and recreates all tables.
    Only for development. Remove or restrict in production.
    """
    db = get_db()
    tables = [
        "point_log", "performance_log", "matches",
        "session_teams", "game_sessions", "rating_history",
    ]
    for t in tables:
        db.execute(f"DROP TABLE IF EXISTS {t}")
    db.commit()
    g.pop("db", None)
    init_db()
    return jsonify({"ok": True, "message": "Database reset (players preserved)"})


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    init_db()
    print("=" * 56)
    print("  🏐  Volleyball Manager  —  app.py")
    print(f"  DB : {DB_PATH}")
    print("  URL: http://localhost:4000")
    print("=" * 56)
    # The app listens on the whole LAN, so the Werkzeug debugger stays off.
    # Set FLASK_DEBUG=1 while developing for auto-reload.
    app.run(host="0.0.0.0", port=4000, debug=os.environ.get("FLASK_DEBUG") == "1")
