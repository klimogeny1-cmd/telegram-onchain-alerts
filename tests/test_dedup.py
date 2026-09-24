"""Tests for alerts_bot.dedup - an in-memory SQLite database per test, no files touched."""
import unittest
from datetime import datetime, timezone

from alerts_bot.dedup import DedupStore


class FakePair:
    def __init__(self, chain_id, pair_address):
        self.chain_id = chain_id
        self.pair_address = pair_address


class DedupStoreTests(unittest.TestCase):
    def setUp(self):
        self.clock_value = datetime(2026, 1, 1, 10, 30, tzinfo=timezone.utc)
        self.store = DedupStore(":memory:", clock=lambda: self.clock_value)

    def tearDown(self):
        self.store.close()

    def test_unseen_pair_is_not_already_sent(self):
        self.assertFalse(self.store.already_sent("new_pair", "solana", "pairAAAA"))

    def test_marking_sent_makes_it_already_sent(self):
        self.store.mark_sent("new_pair", "solana", "pairAAAA")
        self.assertTrue(self.store.already_sent("new_pair", "solana", "pairAAAA"))

    def test_new_pair_dedup_never_expires(self):
        self.store.mark_sent("new_pair", "solana", "pairAAAA")
        self.clock_value = datetime(2030, 6, 1, tzinfo=timezone.utc)  # years later
        self.assertTrue(self.store.already_sent("new_pair", "solana", "pairAAAA"))

    def test_volume_spike_dedup_is_scoped_to_the_clock_hour(self):
        self.store.mark_sent("volume_spike", "solana", "pairBBBB")
        self.assertTrue(self.store.already_sent("volume_spike", "solana", "pairBBBB"))
        self.clock_value = datetime(2026, 1, 1, 11, 5, tzinfo=timezone.utc)  # next hour
        self.assertFalse(self.store.already_sent("volume_spike", "solana", "pairBBBB"))

    def test_volume_spike_dedup_holds_within_the_same_hour(self):
        self.store.mark_sent("volume_spike", "solana", "pairBBBB")
        self.clock_value = datetime(2026, 1, 1, 10, 55, tzinfo=timezone.utc)  # still same hour
        self.assertTrue(self.store.already_sent("volume_spike", "solana", "pairBBBB"))

    def test_kinds_do_not_interfere_with_each_other(self):
        self.store.mark_sent("new_pair", "solana", "pairAAAA")
        self.assertFalse(self.store.already_sent("volume_spike", "solana", "pairAAAA"))

    def test_chains_do_not_interfere_with_each_other(self):
        self.store.mark_sent("new_pair", "solana", "pairAAAA")
        self.assertFalse(self.store.already_sent("new_pair", "base", "pairAAAA"))

    def test_filter_unsent(self):
        a, b = FakePair("solana", "pairAAAA"), FakePair("solana", "pairBBBB")
        self.store.mark_sent("new_pair", "solana", "pairAAAA")
        result = self.store.filter_unsent("new_pair", [a, b])
        self.assertEqual([p.pair_address for p in result], ["pairBBBB"])

    def test_mark_sent_is_idempotent(self):
        self.store.mark_sent("new_pair", "solana", "pairAAAA")
        self.store.mark_sent("new_pair", "solana", "pairAAAA")  # must not raise
        self.assertTrue(self.store.already_sent("new_pair", "solana", "pairAAAA"))


if __name__ == "__main__":
    unittest.main()
