"""Offline checks of the explicitly selected shared HTTP rate staircase."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

import ozon_fresh_fast as core
from ozon_backoff import BackoffGate
from ozon_live_parser import prepare_explicit_step_retry


class HttpStepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000.0
        self.event = threading.Event()

    def sleep(self, seconds):
        self.now += seconds

    def gate(self):
        return BackoffGate(self.root, self.event, 1.1, max_interval=4,
                           http_error_steps=(2, 3, 4), clock=lambda: self.now, sleep=self.sleep)

    def test_mixed_http_failures_raise_exact_steps_and_stop_at_four(self):
        gate = self.gate()
        self.assertEqual(gate.interval, 1.1)
        for reason, expected in (('HTTP403', 2), ('HTTP429', 3), ('HTTP 403', 4), ('HTTP 429', 4)):
            gate.penalize(reason)
            self.assertEqual(gate.interval, expected)
            self.assertEqual(gate.until, self.now + expected)
            gate.wait()

    def test_seven_simultaneous_reader_failures_use_one_shared_step(self):
        gate = self.gate()
        for _ in range(7):
            gate.penalize('HTTP403')
        self.assertEqual((gate.interval, gate.failures, gate.http_error_level), (2, 1, 1))
        self.assertEqual(gate.until, 1002)

    def test_restart_keeps_level_and_next_error_advances_once(self):
        gate = self.gate()
        gate.penalize('HTTP403'); gate.wait()
        gate.penalize('HTTP429'); gate.wait()
        resumed = self.gate()
        self.assertEqual(resumed.interval, 3)
        resumed.penalize('HTTP403')
        self.assertEqual(resumed.interval, 4)

    def test_success_does_not_lower_the_selected_step(self):
        gate = self.gate()
        gate.penalize('HTTP403'); gate.wait()
        for _ in range(50):
            self.now += 20
            gate.note_success()
        self.assertEqual(gate.interval, 2)
        self.assertEqual(self.gate().interval, 2)

    def test_new_policy_replaces_expired_five_second_rate_once(self):
        core.write_json(self.root/'backoff_status.json', dict(request_interval_seconds=5,
            minimum_request_interval_seconds=1.1, maximum_request_interval_seconds=5,
            fixed_403_interval_active=True, on_403_interval_seconds=5,
            retry_at_unix=900, last_failure_unix=800, consecutive_failures=238,
            total_protection_failures=360, stop_reason='HTTP403'))
        gate = self.gate()
        self.assertEqual((gate.interval, gate.failures, gate.total_failures), (1.1, 238, 360))
        gate.wait(); gate.penalize('HTTP429')
        self.assertEqual(gate.interval, 2)

    def test_policy_change_cannot_remove_existing_retry_after_or_manual_check(self):
        core.write_json(self.root/'backoff_status.json', dict(request_interval_seconds=5,
            retry_at_unix=1200, manual_required_since_unix=900, last_failure_unix=900))
        gate = self.gate()
        self.assertEqual((gate.interval, gate.until, gate.manual_since), (1.1, 1200, 900))

    def test_server_retry_after_can_exceed_regular_interval_cap(self):
        gate = self.gate()
        gate.penalize('HTTP429', retry_after=60)
        self.assertEqual((gate.interval, gate.until), (2, 1060))
        gate.wait()
        self.assertEqual(self.now, 1060)
        gate.penalize('HTTP403')
        self.assertEqual(gate.interval, 3)

    def test_captcha_needs_a_new_manual_confirmation(self):
        gate = self.gate()
        gate.penalize('CAPTCHA in HTTP403', manual=True)
        core.write_json(gate.signal, {'confirmed_at_unix': 999})
        self.event.set()
        with self.assertRaises(core.BudgetError):
            gate.wait()
        self.assertEqual(gate.manual_since, 1000)
        self.event.clear()
        core.write_json(gate.signal, {'confirmed_at_unix': 1001})
        gate.wait()
        self.assertEqual((gate.manual_since, self.now, gate.interval), (0, 1002, 2))

    def test_legacy_reader_minimum_is_replaced_without_a_site_request(self):
        gate = self.gate()
        reader = SimpleNamespace(interval=5, base_interval=1.1,
            state={'blocked': True, 'min_interval': 5}, state_path=self.root/'reader.json')
        gate.clear_after_wait(reader)
        self.assertEqual(reader.interval, 1.1)
        self.assertEqual(reader.state['min_interval'], 1.1)
        self.assertFalse(reader.state['blocked'])

    def test_saved_failure_replay_cannot_create_another_step(self):
        gate = self.gate()
        gate.penalize('HTTP403', observed_at=950); gate.wait()
        resumed = self.gate()
        resumed.penalize('Earlier HTTP403', observed_at=950)
        self.assertEqual((resumed.interval, resumed.http_error_level, resumed.failures), (2, 1, 1))

    def test_invalid_or_mixed_policies_are_rejected(self):
        for steps in ((), (3, 2, 4), (2, 2, 4), (2, 5), (float('nan'),), (1, 2)):
            with self.subTest(steps=steps), self.assertRaises(ValueError):
                BackoffGate(self.root, self.event, 1.1, max_interval=4, http_error_steps=steps)
        with self.assertRaises(ValueError):
            BackoffGate(self.root, self.event, 1.1, max_interval=4,
                        http_error_steps=(2, 3, 4), on_403_interval=2)

    def test_explicit_stopped_run_manual_confirmation_preserves_server_wait(self):
        gate = self.gate()
        gate.penalize('CAPTCHA in HTTP403', manual=True, retry_after=60)
        with patch('ozon_live_parser.time.time', return_value=1001):
            prepare_explicit_step_retry(gate, SimpleNamespace(retry_after_pause=True,
                manual_access_confirmed=False))
            self.assertFalse(gate.signal.exists())
            prepare_explicit_step_retry(gate, SimpleNamespace(retry_after_pause=True,
                manual_access_confirmed=True))
        gate.wait()
        self.assertEqual((gate.manual_since, self.now), (0, 1060))


if __name__ == '__main__':
    unittest.main()
