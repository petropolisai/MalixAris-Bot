import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from modules import backup_feature


class BackupFeatureTests(unittest.TestCase):
    def test_archive_excludes_secrets_and_restores_only_known_state_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            data_dir.mkdir()
            events_path = data_dir / "events.json"
            db_path = data_dir / "vouch_bot.db"
            ai_path = data_dir / "metal_ai_data.json"
            token_shape = "A" * 24 + "." + "B" * 6 + "." + "C" * 32
            events_path.write_text("[]", encoding="utf-8")
            ai_path.write_text(json.dumps({"maintenance_mode": False, "note": token_shape}), encoding="utf-8")
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO sample(value) VALUES (?)", (f"safe test row {token_shape}",))
            connection.commit()
            connection.close()

            env = {
                "DATA_DIR": str(data_dir),
                "STATE_PATH": str(events_path),
                "DB_PATH": str(db_path),
                "DATA_FILE": str(ai_path),
                "BOT_NAME": "MalixAris Bot",
                "DISCORD_TOKEN": "NOT-A-REAL-DISCORD-TOKEN",
                "OPENROUTER_API_KEY": "NOT-A-REAL-API-KEY",
            }
            with patch.dict(os.environ, env, clear=False):
                artifact = backup_feature.build_backup_artifact()
                with zipfile.ZipFile(io.BytesIO(artifact.data)) as archive:
                    names = set(archive.namelist())
                    self.assertEqual(
                        names,
                        {
                            "manifest.json",
                            "settings.json",
                            "data/events.json",
                            "data/vouch_bot.db",
                            "data/metal_ai_data.json",
                        },
                    )
                    settings = json.loads(archive.read("settings.json"))
                    self.assertEqual(settings["BOT_NAME"], "MalixAris Bot")
                    self.assertNotIn("DISCORD_TOKEN", settings)
                    self.assertNotIn("OPENROUTER_API_KEY", settings)
                self.assertNotIn(b"NOT-A-REAL-DISCORD-TOKEN", artifact.data)
                self.assertNotIn(b"NOT-A-REAL-API-KEY", artifact.data)
                self.assertNotIn(token_shape.encode("ascii"), artifact.data)

                events_path.unlink()
                db_path.unlink()
                ai_path.unlink()
                restored = backup_feature.restore_backup_archive(
                    artifact.data,
                    missing_only=True,
                    force_settings=False,
                )
                self.assertEqual(len(restored["restored_files"]), 3)
                self.assertEqual(json.loads(events_path.read_text(encoding="utf-8")), [])
                restored_ai = json.loads(ai_path.read_text(encoding="utf-8"))
                self.assertEqual(restored_ai["maintenance_mode"], False)
                self.assertEqual(restored_ai["note"], "[redacted token-like string]")
                check = sqlite3.connect(db_path)
                row = check.execute("SELECT value FROM sample").fetchone()
                check.close()
                self.assertEqual(row[0], "safe test row [redacted token-like string]")

    def test_safe_settings_allowlist_never_contains_credentials(self):
        with patch.dict(
            os.environ,
            {
                "DISCORD_TOKEN": "TOKEN-MUST-NOT-BE-BACKED-UP",
                "OPENROUTER_API_KEY": "KEY-MUST-NOT-BE-BACKED-UP",
                "AI_API_KEYS": "ALSO-SECRET",
                "BOT_NAME": "Test Bot",
            },
            clear=False,
        ):
            settings = backup_feature._safe_settings()
        self.assertEqual(settings.get("BOT_NAME"), "Test Bot")
        self.assertNotIn("DISCORD_TOKEN", settings)
        self.assertNotIn("OPENROUTER_API_KEY", settings)
        self.assertNotIn("AI_API_KEYS", settings)
        self.assertNotIn("TOKEN-MUST-NOT-BE-BACKED-UP", settings.values())
        self.assertNotIn("KEY-MUST-NOT-BE-BACKED-UP", settings.values())

    def test_restore_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            data_dir.mkdir()
            env = {
                "DATA_DIR": str(data_dir),
                "STATE_PATH": str(data_dir / "events.json"),
                "DB_PATH": str(data_dir / "vouch_bot.db"),
                "DATA_FILE": str(data_dir / "metal_ai_data.json"),
            }
            malicious = io.BytesIO()
            with zipfile.ZipFile(malicious, "w") as archive:
                archive.writestr("manifest.json", "{}")
                archive.writestr("settings.json", "{}")
                archive.writestr("../.env", "SHOULD_NOT_BE_EXTRACTED")
            with patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(ValueError, "unsafe file path"):
                    backup_feature.restore_backup_archive(malicious.getvalue())


if __name__ == "__main__":
    unittest.main()
