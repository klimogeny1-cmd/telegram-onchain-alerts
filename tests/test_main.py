"""End-to-end tests for main.py - config checks and the offline replay demo. Temp .env
files only; the replay path never opens a socket."""
import contextlib
import io
import os
import signal
import tempfile
import unittest

import main
from tests.solami_helpers import BLUR_FRAMES, DEMO

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "123456789:AAEfakeTokenFakeTokenFakeTokenFak"


class MainTests(unittest.TestCase):
    def setUp(self):
        # main() installs SIGTERM/SIGINT handlers; put the originals back afterwards
        self.handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        for sig, handler in self.handlers.items():
            signal.signal(sig, handler)
        self.tmp.cleanup()

    def env(self, text):
        path = os.path.join(self.tmp.name, ".env")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\nSTATE_DB_PATH=%s\n" % os.path.join(self.tmp.name, "state", "seen.db"))
        return path

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main.main(list(argv))
        return code, out.getvalue()

    def test_check_fails_without_a_solami_key(self):
        code, _ = self.run_main("--env", self.env("BOT_TOKEN=%s\nCHANNEL_ID=@c" % TOKEN), "--check")
        self.assertEqual(code, main.CONFIG_ERROR_EXIT_CODE)

    def test_check_passes_with_key_token_and_channel(self):
        env = self.env("BOT_TOKEN=%s\nCHANNEL_ID=@c\nSOLAMI_API_KEY=sk_test_fake\nWATCH_MINTS=%s" % (TOKEN, DEMO))
        self.assertEqual(self.run_main("--env", env, "--check")[0], 0)

    def test_check_still_requires_telegram_settings(self):
        env = self.env("SOLAMI_API_KEY=sk_test_fake")
        self.assertEqual(self.run_main("--env", env, "--check")[0], main.CONFIG_ERROR_EXIT_CODE)

    def test_legacy_dexscreener_check_is_unchanged(self):
        env = self.env("BOT_TOKEN=%s\nCHANNEL_ID=@c\nDATA_SOURCE=dexscreener" % TOKEN)
        self.assertEqual(self.run_main("--env", env, "--check")[0], 0)

    def test_offline_replay_demo_prints_posts(self):
        demo_env = os.path.join(REPO, "examples", "offline-demo.conf")
        code, out = self.run_main("--env", demo_env, "--replay-file", BLUR_FRAMES, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("<b>Solana Tape</b>", out)
        self.assertIn("<b>Tape summary</b>", out)
        self.assertIn("not financial advice", out)
        self.assertEqual(out.count("DRY RUN: would send"), 2)


if __name__ == "__main__":
    unittest.main()
