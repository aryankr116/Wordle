import os
import sqlite3
import tempfile
import unittest
from datetime import date

from app import WORDS, create_admin, create_app, score_guess


class GuessTheWordTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = os.path.join(self.temp_dir.name, "test.sqlite3")
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "DATABASE": self.database,
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def register_and_login(self, username="Player", password="Test1$"):
        response = self.client.post(
            "/register",
            data={"username": username, "password": password},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        return self.client.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=True,
        )

    def database_connection(self):
        return sqlite3.connect(self.database)

    def start_game(self, target="APPLE"):
        response = self.client.post("/game/start", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with self.database_connection() as db:
            game_id = db.execute("SELECT MAX(id) FROM games").fetchone()[0]
            db.execute("UPDATE games SET target_word = ? WHERE id = ?", (target, game_id))
            db.commit()
        return game_id

    def guess(self, game_id, word):
        return self.client.post(
            f"/game/{game_id}/guess",
            data={"guess": word},
            follow_redirects=True,
        )

    def test_database_starts_with_twenty_uppercase_words(self):
        with self.database_connection() as db:
            words = [row[0] for row in db.execute("SELECT word FROM words ORDER BY word")]
        self.assertEqual(len(words), 20)
        self.assertEqual(set(words), set(WORDS))
        self.assertTrue(all(len(word) == 5 and word.isupper() for word in words))

    def test_registration_validation_and_player_role(self):
        for username, password in (
            ("abcd", "Test1$"),
            ("ValidName", "abcde"),
            ("ValidName", "NoDigit$"),
            ("ValidName", "NoSpecial1"),
        ):
            response = self.client.post(
                "/register", data={"username": username, "password": password}
            )
            self.assertEqual(response.status_code, 200)

        with self.database_connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)

        self.register_and_login()
        with self.database_connection() as db:
            self.assertEqual(
                db.execute("SELECT role FROM users WHERE username = 'Player'").fetchone()[0],
                "player",
            )
        self.assertEqual(self.client.get("/admin").status_code, 302)

    def test_guesses_are_uppercase_and_feedback_is_persisted_in_order(self):
        self.register_and_login()
        game_id = self.start_game()
        invalid = self.guess(game_id, "crane")
        self.assertIn(b"Enter exactly 5 uppercase letters", invalid.data)
        self.assertEqual(score_guess("CRANE", "APPLE"), ["grey", "grey", "orange", "grey", "green"])

        response = self.guess(game_id, "CRANE")
        self.assertIn(b'class="tile orange">A</span>', response.data)
        self.assertIn(b'class="tile green">E</span>', response.data)
        self.assertIn(b'name="guess"', response.data)
        response = self.guess(game_id, "APPLE")
        self.assertIn(b"Congratulations, you guessed the word!", response.data)
        self.assertIn(b">OK</a>", response.data)
        self.assertNotIn(b'name="guess"', response.data)

        with self.database_connection() as db:
            guesses = db.execute(
                "SELECT word FROM guesses WHERE game_id = ? ORDER BY guess_number",
                (game_id,),
            ).fetchall()
            state, target, played_on = db.execute(
                "SELECT state, target_word, played_on FROM games WHERE id = ?",
                (game_id,),
            ).fetchone()
        self.assertEqual([row[0] for row in guesses], ["CRANE", "APPLE"])
        self.assertEqual((state, target, played_on), ("won", "APPLE", date.today().isoformat()))

    def test_fifth_wrong_guess_loses_and_daily_limit_is_three_games(self):
        self.register_and_login()
        game_id = self.start_game()
        for word in ("BRAVE", "CHAIR", "DREAM", "FLAME"):
            response = self.guess(game_id, word)
            self.assertIn(b'name="guess"', response.data)
        response = self.guess(game_id, "GRAPE")
        self.assertIn(b"Better luck next time!", response.data)
        self.assertNotIn(b'name="guess"', response.data)

        for _ in range(2):
            game_id = self.start_game()
            self.assertIn(
                b"Congratulations, you guessed the word!",
                self.guess(game_id, "APPLE").data,
            )
        response = self.client.post("/game/start", follow_redirects=True)
        self.assertIn(b"used all 3 games", response.data)
        with self.database_connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM games").fetchone()[0], 3)

    def test_admin_creation_and_reports(self):
        self.register_and_login()
        first_game = self.start_game()
        self.guess(first_game, "APPLE")
        second_game = self.start_game()
        for word in ("BRAVE", "CHAIR", "DREAM", "FLAME", "GRAPE"):
            self.guess(second_game, word)

        create_admin("AdminUser", "Admin1$", self.database)
        admin_client = self.app.test_client()
        login_response = admin_client.post(
            "/login",
            data={"username": "AdminUser", "password": "Admin1$"},
            follow_redirects=True,
        )
        self.assertEqual(login_response.status_code, 200)
        self.assertEqual(admin_client.get("/game").status_code, 302)
        daily = admin_client.get(f"/admin/day?date={date.today().isoformat()}")
        self.assertIn(b"players who played", daily.data)
        self.assertIn(b"games won", daily.data)
        self.assertIn(b">1</strong>", daily.data)
        player = admin_client.get("/admin/user?username=Player")
        self.assertIn(b"Words tried", player.data)
        self.assertIn(b"Correct guesses", player.data)
        self.assertIn(b">2</td>", player.data)
        self.assertIn(b">1</td>", player.data)

    def test_duplicate_letters_only_mark_available_occurrences(self):
        self.assertEqual(
            score_guess("ALLEY", "APPLE"),
            ["green", "orange", "grey", "orange", "grey"],
        )


if __name__ == "__main__":
    unittest.main()
