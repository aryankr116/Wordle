import getpass
import os
import re
import secrets
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import date
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash


WORDS = (
    "APPLE", "BRAVE", "CHAIR", "DREAM", "EARTH",
    "FLAME", "GRAPE", "HEART", "IVORY", "JUICE",
    "KNIFE", "LEMON", "MUSIC", "NIGHT", "OCEAN",
    "PLANT", "QUEEN", "RIVER", "STONE", "TRAIN",
)
USERNAME_PATTERN = re.compile(r"^[A-Za-z]{5,}$")
GUESS_PATTERN = re.compile(r"^[A-Z]{5}$")
PASSWORD_SPECIALS = frozenset("$%*")
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('player', 'admin')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    target_word TEXT NOT NULL,
    played_on TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'playing'
        CHECK (state IN ('playing', 'won', 'lost')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS games_user_date_idx ON games(user_id, played_on);
CREATE TABLE IF NOT EXISTS words (
    word TEXT PRIMARY KEY CHECK (length(word) = 5)
);
CREATE TABLE IF NOT EXISTS guesses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    guess_number INTEGER NOT NULL CHECK (guess_number BETWEEN 1 AND 5),
    word TEXT NOT NULL,
    UNIQUE (game_id, guess_number)
);
"""


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        DATABASE=os.environ.get(
            "DATABASE_PATH", str(Path(app.instance_path) / "wordle.sqlite3")
        ),
    )
    if test_config:
        app.config.update(test_config)
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(app.config["DATABASE"])) as db:
        db.executescript(SCHEMA)
        db.executemany("INSERT OR IGNORE INTO words(word) VALUES (?)", [(word,) for word in WORDS])
        db.commit()

    @app.before_request
    def load_user():
        user_id = session.get("user_id")
        g.user = None
        if user_id is not None:
            g.user = get_db().execute(
                "SELECT id, username, role FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if g.user is None:
                session.clear()

    @app.teardown_appcontext
    def close_db(_error=None):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login"))
            return view(*args, **kwargs)

        return wrapped

    def player_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login"))
            if g.user["role"] != "player":
                flash("A player account is required to play.", "error")
                return redirect(url_for("admin"))
            return view(*args, **kwargs)

        return wrapped

    def admin_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login"))
            if g.user["role"] != "admin":
                flash("Administrator access is required.", "error")
                return redirect(url_for("game"))
            return view(*args, **kwargs)

        return wrapped

    @app.route("/")
    def index():
        if g.user is None:
            return redirect(url_for("login"))
        return redirect(url_for("admin" if g.user["role"] == "admin" else "game"))

    @app.route("/register", methods=("GET", "POST"))
    def register():
        if g.user:
            return redirect(url_for("admin" if g.user["role"] == "admin" else "game"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if not USERNAME_PATTERN.fullmatch(username):
                flash("Username must contain at least 5 letters (A-Z).", "error")
            elif (
                len(password) < 5
                or not any(character.isalpha() for character in password)
                or not any(character.isdigit() for character in password)
                or not any(character in PASSWORD_SPECIALS for character in password)
            ):
                flash(
                    "Password must be at least 5 characters and include a letter, "
                    "a number, and one of $, % or *.",
                    "error",
                )
            else:
                try:
                    get_db().execute(
                        "INSERT INTO users(username, password_hash, role) VALUES (?, ?, 'player')",
                        (username, generate_password_hash(password)),
                    )
                    get_db().commit()
                except sqlite3.IntegrityError:
                    flash("That username is already registered.", "error")
                else:
                    flash("Registration successful. Please log in.", "success")
                    return redirect(url_for("login"))
        return render_template("register.html")

    @app.route("/login", methods=("GET", "POST"))
    def login():
        if g.user:
            return redirect(url_for("admin" if g.user["role"] == "admin" else "game"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = get_db().execute(
                "SELECT id, password_hash, role FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if user is None or not check_password_hash(user["password_hash"], password):
                flash("Invalid username or password.", "error")
            else:
                session.clear()
                session["user_id"] = user["id"]
                return redirect(url_for("admin" if user["role"] == "admin" else "game"))
        return render_template("login.html")

    @app.post("/logout")
    @login_required
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/game")
    @player_required
    def game():
        current_game = get_db().execute(
            """SELECT id, target_word, played_on, state FROM games
               WHERE user_id = ? AND state = 'playing'
               ORDER BY id DESC LIMIT 1""",
            (g.user["id"],),
        ).fetchone()
        guesses = []
        remaining_games = _remaining_games(g.user["id"])
        if current_game:
            guesses = get_db().execute(
                "SELECT word, guess_number FROM guesses WHERE game_id = ? ORDER BY guess_number",
                (current_game["id"],),
            ).fetchall()
        guess_rows = [
            {"word": row["word"], "feedback": score_guess(row["word"], current_game["target_word"])}
            for row in guesses
        ] if current_game else []
        return render_template(
            "game.html",
            current_game=current_game,
            guesses=guess_rows,
            remaining_games=remaining_games,
        )

    @app.post("/game/start")
    @player_required
    def start_game():
        db = get_db()
        today = date.today().isoformat()
        with db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                """SELECT id FROM games
                   WHERE user_id = ? AND state = 'playing'
                   ORDER BY id DESC LIMIT 1""",
                (g.user["id"],),
            ).fetchone()
            if existing:
                db.commit()
                return redirect(url_for("game"))
            games_today = db.execute(
                "SELECT COUNT(*) FROM games WHERE user_id = ? AND played_on = ?",
                (g.user["id"], today),
            ).fetchone()[0]
            if games_today >= 3:
                db.rollback()
                flash("You have used all 3 games for today. Please come back tomorrow.", "error")
                return redirect(url_for("game"))
            word = db.execute(
                "SELECT word FROM words ORDER BY RANDOM() LIMIT 1"
            ).fetchone()
            if word is None:
                raise RuntimeError("The word list is empty; initialize the database first.")
            db.execute(
                "INSERT INTO games(user_id, target_word, played_on) VALUES (?, ?, ?)",
                (g.user["id"], word["word"], today),
            )
            db.commit()
        return redirect(url_for("game"))

    @app.post("/game/<int:game_id>/guess")
    @player_required
    def submit_guess(game_id):
        word = request.form.get("guess", "")
        if not GUESS_PATTERN.fullmatch(word):
            flash("Enter exactly 5 uppercase letters (A-Z).", "error")
            return redirect(url_for("game"))

        db = get_db()
        with db:
            db.execute("BEGIN IMMEDIATE")
            current_game = db.execute(
                "SELECT id, target_word, state FROM games WHERE id = ? AND user_id = ?",
                (game_id, g.user["id"]),
            ).fetchone()
            if current_game is None or current_game["state"] != "playing":
                db.rollback()
                flash("That game is no longer active.", "error")
                return redirect(url_for("game"))
            guess_count = db.execute(
                "SELECT COUNT(*) FROM guesses WHERE game_id = ?", (game_id,)
            ).fetchone()[0]
            if guess_count >= 5:
                db.rollback()
                flash("You have used all 5 guesses for this game.", "error")
                return redirect(url_for("game"))

            guess_number = guess_count + 1
            db.execute(
                "INSERT INTO guesses(game_id, guess_number, word) VALUES (?, ?, ?)",
                (game_id, guess_number, word),
            )
            state = "won" if word == current_game["target_word"] else (
                "lost" if guess_number == 5 else "playing"
            )
            db.execute("UPDATE games SET state = ? WHERE id = ?", (state, game_id))
            db.commit()
        guesses = db.execute(
            "SELECT word, guess_number FROM guesses WHERE game_id = ? ORDER BY guess_number",
            (game_id,),
        ).fetchall()
        guess_rows = [
            {"word": row["word"], "feedback": score_guess(row["word"], current_game["target_word"])}
            for row in guesses
        ]
        message = (
            "Congratulations, you guessed the word!"
            if state == "won"
            else "Better luck next time!"
            if state == "lost"
            else None
        )
        return render_template(
            "game.html",
            current_game=current_game,
            guesses=guess_rows,
            remaining_games=_remaining_games(g.user["id"]),
            result_message=message,
            finished=state in ("won", "lost"),
        )

    @app.route("/admin")
    @admin_required
    def admin():
        return render_template("admin.html")

    @app.route("/admin/day", methods=("GET", "POST"))
    @admin_required
    def admin_day():
        selected_date = request.values.get("date", date.today().isoformat())
        if not _valid_date(selected_date):
            flash("Enter a valid date.", "error")
            selected_date = date.today().isoformat()
        report = get_db().execute(
            """SELECT COUNT(DISTINCT user_id) AS users,
                      SUM(CASE WHEN state = 'won' THEN 1 ELSE 0 END) AS correct_guesses
               FROM games WHERE played_on = ?""",
            (selected_date,),
        ).fetchone()
        return render_template("admin_day.html", selected_date=selected_date, report=report)

    @app.route("/admin/user", methods=("GET", "POST"))
    @admin_required
    def admin_user():
        selected_user = request.values.get("username", "").strip()
        users = get_db().execute(
            "SELECT username FROM users WHERE role = 'player' ORDER BY username COLLATE NOCASE"
        ).fetchall()
        report = []
        if selected_user:
            user = get_db().execute(
                "SELECT id FROM users WHERE username = ? AND role = 'player'",
                (selected_user,),
            ).fetchone()
            if user is None:
                flash("Player not found.", "error")
            else:
                report = get_db().execute(
                    """SELECT played_on AS date,
                              COUNT(*) AS words_tried,
                              SUM(CASE WHEN state = 'won' THEN 1 ELSE 0 END) AS correct_guesses
                       FROM games WHERE user_id = ?
                       GROUP BY played_on ORDER BY played_on DESC""",
                    (user["id"],),
                ).fetchall()
        return render_template(
            "admin_user.html",
            users=users,
            selected_user=selected_user,
            report=report,
        )

    return app


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def _remaining_games(user_id):
    count = get_db().execute(
        "SELECT COUNT(*) FROM games WHERE user_id = ? AND played_on = ?",
        (user_id, date.today().isoformat()),
    ).fetchone()[0]
    return max(0, 3 - count)


def _valid_date(value):
    try:
        return date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError):
        return False


def score_guess(guess, target):
    result = ["grey"] * len(guess)
    remaining = Counter()
    for index, letter in enumerate(target):
        if guess[index] == letter:
            result[index] = "green"
        else:
            remaining[letter] += 1
    for index, letter in enumerate(guess):
        if result[index] != "green" and remaining[letter] > 0:
            result[index] = "orange"
            remaining[letter] -= 1
    return result


def create_admin(username, password, database):
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError("Admin username must contain at least 5 letters (A-Z).")
    if (
        len(password) < 5
        or not any(character.isalpha() for character in password)
        or not any(character.isdigit() for character in password)
        or not any(character in PASSWORD_SPECIALS for character in password)
    ):
        raise ValueError("Password must include 5 characters, a letter, a number, and $, % or *.")
    db = sqlite3.connect(database)
    try:
        db.executescript(SCHEMA)
        db.executemany("INSERT OR IGNORE INTO words(word) VALUES (?)", [(word,) for word in WORDS])
        db.execute(
            "INSERT INTO users(username, password_hash, role) VALUES (?, ?, 'admin')",
            (username, generate_password_hash(password)),
        )
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "create-admin":
        if len(sys.argv) != 3:
            raise SystemExit("Usage: python app.py create-admin USERNAME")
        database = os.environ.get(
            "DATABASE_PATH", str(Path(__file__).parent / "instance" / "wordle.sqlite3")
        )
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        password = getpass.getpass("Admin password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            raise SystemExit("Passwords do not match.")
        try:
            create_admin(sys.argv[2], password, database)
        except (ValueError, sqlite3.IntegrityError) as error:
            raise SystemExit(str(error))
        print(f"Administrator {sys.argv[2]} created.")
    else:
        create_app().run(debug=os.environ.get("FLASK_DEBUG") == "1")
