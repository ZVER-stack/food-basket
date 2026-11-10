import threading
import unittest
from unittest.mock import Mock

from selenium.common.exceptions import WebDriverException

from samokat.core import Blocked
from samokat.firefox import FirefoxAPI


class ShutdownTests(unittest.TestCase):
    def api(self):
        api = FirefoxAPI.__new__(FirefoxAPI)
        api.stopped = threading.Event()
        api.reason = ''
        api.driver = None
        api.condition = threading.Condition()
        api.waiters, api.ticket, api.pending = [], 0, 0
        return api

    def request(self, path):
        request = Mock()
        request._params = {'isBlocked': True}
        request.url = 'https://api-web.samokat.ru' + path
        request.fail.side_effect = WebDriverException('no such request: Blocked request not found')
        return request

    def test_native_callback_after_close_does_not_escape_or_retry(self):
        api = self.api()
        api.stopped.set()
        request = self.request('/payments/types')
        api._before(request)
        request.fail.assert_called_once()
        request.continue_request.assert_not_called()
        self.assertEqual(api.reason, '')

    def test_shutdown_callback_preserves_original_block_reason(self):
        api = self.api()
        api._block('HTTP 403')
        api._before(self.request('/oauth/token/refresh'))
        self.assertEqual(api.reason, 'HTTP 403')
        with self.assertRaises(Blocked):
            api.check()

    def test_expired_catalog_request_also_cleans_up_queue(self):
        api = self.api()
        api.stopped.set()
        request = self.request('/showcases/list')
        api._before(request)
        self.assertEqual(api.pending, 0)
        self.assertEqual(api.waiters, [])
        request.fail.assert_called_once()

    def test_real_transport_failure_still_stops_active_collection(self):
        api = self.api()
        request = self.request('/payments/types')
        request.continue_request.side_effect = WebDriverException('connection lost')
        api._before(request)
        self.assertTrue(api.stopped.is_set())
        with self.assertRaises(Blocked):
            api.check()


if __name__ == '__main__':
    unittest.main()
