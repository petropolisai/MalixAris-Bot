import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from modules import backup_feature


class BackupRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_lists_prior_files_but_never_restores_them(self):
        bot = SimpleNamespace(user=SimpleNamespace(id=123))
        channel = SimpleNamespace(send=AsyncMock())
        service = backup_feature.BackupService(bot)
        prior_message = SimpleNamespace(
            id=456,
            created_at=datetime.now(timezone.utc),
            attachments=[
                SimpleNamespace(
                    filename=f"{backup_feature.BACKUP_PREFIX}prior.zip",
                    size=1024,
                )
            ],
        )

        with (
            patch.object(backup_feature, "_get_channel", new=AsyncMock(return_value=channel)),
            patch.object(service, "_backup_messages", new=AsyncMock(return_value=[prior_message])),
            patch.object(backup_feature, "build_backup_artifact", return_value=SimpleNamespace(fingerprint="local-state")),
            patch.object(backup_feature, "restore_backup_archive") as restore_archive,
            patch.object(service, "create_backup", new=AsyncMock()) as create_backup,
        ):
            await service.start()
            await service.close()

        self.assertTrue(service._closing)
        self.assertIn("Nothing was restored automatically", service.startup_summary)
        restore_archive.assert_not_called()
        create_backup.assert_not_awaited()
        channel.send.assert_awaited_once()
        self.assertFalse(hasattr(backup_feature, "_check_interval"))
        self.assertFalse(hasattr(service, "_watch_task"))

    async def test_successful_state_notifications_are_debounced(self):
        bot = SimpleNamespace()
        service = backup_feature.BackupService(bot)
        service.started = True
        service._loop = asyncio.get_running_loop()

        with (
            patch.object(backup_feature, "BACKUP_DEBOUNCE_SECONDS", 0.01),
            patch.object(service, "create_backup", new=AsyncMock()) as create_backup,
        ):
            service.notify_state_changed("AI state saved")
            await asyncio.sleep(0.002)
            service.notify_state_changed("vouch database committed")
            task = service._change_task
            self.assertIsNotNone(task)
            await task

        create_backup.assert_awaited_once()
        self.assertFalse(create_backup.await_args.kwargs["force"])
        self.assertFalse(hasattr(service, "_watch_task"))
        await service.close()


if __name__ == "__main__":
    unittest.main()
