"""Test package for telegram-onchain-alerts.

Silences library logging (runner.py and dexscreener.py both log warnings for edge cases
that individual tests deliberately trigger) so `python3 -m unittest` output stays
readable. No test here relies on assertLogs, so this is safe process-wide.
"""
import logging

logging.disable(logging.CRITICAL)
