import unittest

import discord
from discord import app_commands

import main


EXPECTED_PATHS = {
    "/apply panel",
    "/apply closeticket",
    "/apply diagnose",
    "/ticket panel",
    "/backup status",
    "/backup now",
    "/backup restore",
    "/gtn number start",
    "/gtn number status",
    "/gtn number cancel",
    "/gtn number reveal",
    "/gtn race start",
    "/gtn race status",
    "/gtn race cancel",
    "/gtn help",
    "/vouch add",
    "/vouch view",
    "/vouch edit",
    "/vouch remove",
    "/vouch import",
    "/vouch blacklist add",
    "/vouch blacklist remove",
    "/vouch profile",
    "/vouch leaderboard",
    "/vouch stats",
    "/vouch setup",
    "/vouch help",
    "/ai chat",
    "/ai ask",
    "/ai reset",
    "/ai roast",
    "/ai vibe",
    "/ai mode",
    "/ai whoami",
    "/ai about",
    "/ai describe",
    "/ai testvision",
    "/ai serverinfo",
    "/ai summarize",
    "/ai rules",
    "/ai askrules",
    "/ai notify",
    "/ai pingsettings",
    "/ai ping",
    "/ai help",
    "/ai stats",
    "/ai persona",
    "/ai resetpersona",
    "/ai models",
    "/ai dashboard",
    "/ai server setrules",
    "/ai server addrule",
    "/ai server removerule",
    "/ai server clearrules",
    "/ai server setaichannel",
    "/ai server removeaichannel",
    "/ai server listaichannels",
    "/ai server pingpolicy",
    "/ai server pingprotect",
    "/ai server pingunprotect",
    "/ai server setdailylimit",
    "/ai server setlogchannel",
    "/ai server removelogchannel",
    "/ai admin addowner",
    "/ai admin removeowner",
    "/ai admin owners",
    "/ai admin clear",
    "/ai admin addpremium",
    "/ai admin removepremium",
    "/ai admin blacklist",
    "/ai admin unblacklist",
    "/ai admin userinfo",
    "/ai admin setmodel",
    "/ai admin resetmodel",
    "/ai admin diagnose",
    "/ai admin apistatus",
    "/ai admin maintenance",
    "/ai admin broadcast",
}


def command_paths(node, prefix=()):
    path = prefix + (node.name,)
    if isinstance(node, app_commands.Group):
        for child in node.commands:
            yield from command_paths(child, path)
    else:
        yield "/" + " ".join(path)


class CommandTreeTests(unittest.TestCase):
    def test_all_expected_leaf_commands_are_registered_once(self):
        paths = [
            path
            for root in main.bot.tree.get_commands()
            for path in command_paths(root)
        ]
        self.assertEqual(len(paths), 78)
        self.assertEqual(len(set(paths)), 78)
        self.assertEqual(set(paths), EXPECTED_PATHS)

    def test_six_root_namespaces_are_registered(self):
        self.assertEqual(
            {command.name for command in main.bot.tree.get_commands()},
            {"apply", "gtn", "vouch", "ai", "ticket", "backup"},
        )

    def test_ai_root_respects_discord_option_limit(self):
        self.assertEqual(len(main.ai_group.commands), 25)
        self.assertLessEqual(len(main.ai_server_group.commands), 25)
        self.assertLessEqual(len(main.ai_admin_group.commands), 25)

    def test_command_tree_serializes_for_discord_api(self):
        for command in main.bot.tree.get_commands():
            payload = command.to_dict(main.bot.tree)
            self.assertEqual(payload["name"], command.name)
            self.assertLessEqual(len(payload.get("options", [])), 25)

    def test_persistent_views_remain_persistent(self):
        for view in (
            main.apply_feature.PanelView(),
            main.apply_feature.ReviewView(),
            main.apply_feature.CloseTicketView(),
            main.events_feature.NumberControlView(),
            main.events_feature.RaceControlView(),
            main.ticket_feature.TicketPanelView(),
            main.ticket_feature.SupportTicketControls(),
        ):
            self.assertTrue(view.is_persistent(), type(view).__name__)

    def test_coexisting_message_and_ready_listeners_are_registered(self):
        self.assertEqual(len(main.bot.extra_events["on_message"]), 3)
        self.assertEqual(len(main.bot.extra_events["on_ready"]), 4)
        self.assertIn("on_member_remove", main.bot.extra_events)
        self.assertIn("on_guild_channel_delete", main.bot.extra_events)

    def test_application_and_support_dropdown_choices(self):
        self.assertEqual(
            [position["name"] for position in main.apply_feature.POSITIONS.values()],
            ["Staff", "Dropper"],
        )
        self.assertEqual(
            set(main.ticket_feature.TICKET_TYPES),
            {"general", "claim", "reward_claim", "support", "partnership", "other"},
        )

    def test_ticket_transcript_redacts_token_shaped_content(self):
        synthetic_token_shape = "A" * 24 + "." + "B" * 6 + "." + "C" * 32
        redacted = main.ticket_feature._redact_token_like_text(
            f"Please do not share this: {synthetic_token_shape}"
        )
        self.assertNotIn(synthetic_token_shape, redacted)
        self.assertIn("[redacted token-like string]", redacted)


if __name__ == "__main__":
    unittest.main()
