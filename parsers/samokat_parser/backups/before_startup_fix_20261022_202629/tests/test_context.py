import threading
import unittest
from unittest.mock import Mock, patch

from selenium.common.exceptions import JavascriptException

from samokat.core import Blocked, ParserError, SchemaError
from samokat.firefox import FirefoxAPI


class ContextTests(unittest.TestCase):
    def api(self):
        api = FirefoxAPI.__new__(FirefoxAPI)
        api.stopped = threading.Event()
        api.reason = ''
        api.capture_dir = None
        api.startup_timeout = 60
        api.driver = Mock()
        api.driver.current_url = 'https://samokat.ru/'
        api.driver.execute_script.return_value = ''
        return api

    def test_fetch_error_then_page_reload_recovers_and_preserves_request(self):
        api = self.api()
        api.driver.execute_async_script.side_effect = [
            {'error': {'status': 'FETCH_ERROR'}},
            {'bridgeState': 'missing'}, {'data': [{'showcaseId': 's'}]},
        ]
        with patch.object(api, '_wait_for_bridge') as restore, patch('samokat.firefox.time.sleep'):
            self.assertEqual(api.query('/showcases/list', params={'lat': 55.7, 'lon': 37.6}), [{'showcaseId': 's'}])
        restore.assert_called_once_with(60)
        self.assertEqual(api.driver.execute_async_script.call_count, 3)
        requests = [call.args[1] for call in api.driver.execute_async_script.call_args_list]
        self.assertTrue(all(request == requests[0] for request in requests))

    def test_document_unloaded_during_async_execution_is_recovered(self):
        api = self.api()
        api.driver.execute_async_script.side_effect = [JavascriptException('Document was unloaded'), {'data': []}]
        with patch.object(api, '_wait_for_bridge') as restore:
            self.assertEqual(api.query('/showcases/list'), [])
        restore.assert_called_once()

    def test_context_recovery_is_bounded(self):
        api = self.api()
        api.driver.execute_async_script.return_value = {'bridgeState': 'missing'}
        with patch.object(api, '_wait_for_bridge') as restore, self.assertRaises(ParserError):
            api.query('/showcases/list')
        self.assertEqual(restore.call_count, 2)
        self.assertEqual(api.driver.execute_async_script.call_count, 3)

    def test_403_after_recovery_stops_immediately(self):
        api = self.api()
        api.driver.execute_async_script.side_effect = [{'bridgeState': 'missing'}, {'error': {'status': 403}}]
        with patch.object(api, '_wait_for_bridge') as restore, self.assertRaises(Blocked):
            api.query('/showcases/list')
        restore.assert_called_once()
        self.assertEqual(api.driver.execute_async_script.call_count, 2)

    def test_captcha_during_recovery_is_not_retried(self):
        api = self.api()
        api.driver.execute_async_script.return_value = {'bridgeState': 'missing'}
        with patch.object(api, '_wait_for_bridge', side_effect=Blocked('captcha')) as restore, self.assertRaises(Blocked):
            api.query('/showcases/list')
        restore.assert_called_once()
        self.assertEqual(api.driver.execute_async_script.call_count, 1)

    def test_real_adapter_js_error_is_not_classified_as_navigation(self):
        api = self.api()
        api.driver.execute_async_script.side_effect = JavascriptException('TypeError: cannot read properties')
        with patch.object(api, '_wait_for_bridge') as restore, self.assertRaises(JavascriptException):
            api.query('/showcases/list')
        restore.assert_not_called()

    def test_unknown_bridge_error_is_not_retried(self):
        api = self.api()
        api.driver.execute_async_script.return_value = {'bridgeError': 'Изменился клиент публичного API'}
        with patch.object(api, '_wait_for_bridge') as restore, self.assertRaises(SchemaError):
            api.query('/showcases/list')
        restore.assert_not_called()

    def test_wait_requires_ready_guest_and_stable_document(self):
        api = self.api()
        # Каждый шаг: check() читает видимый текст, затем bridge и timeOrigin.
        api.driver.execute_script.side_effect = ['', {'ready': False},
                                                 '', {'ready': True, 'version': '2.86.1'}, 123,
                                                 '', {'ready': True, 'version': '2.86.1'}, 123]
        with patch('samokat.firefox.time.sleep'):
            api._wait_for_bridge(60)
        self.assertEqual(api.version, '2.86.1')

    def test_wait_respects_version_or_authorization_error(self):
        for error in ('Изменилась версия web-клиента', 'Авторизованная сессия запрещена'):
            api = self.api()
            api.driver.execute_script.side_effect = ['', {'error': error}]
            with self.subTest(error=error), self.assertRaises(SchemaError):
                api._wait_for_bridge(60)

    def test_wait_does_not_reinstall_on_another_site(self):
        api = self.api()
        api.driver.current_url = 'https://example.org/'
        with self.assertRaises(ParserError):
            api._wait_for_bridge(60)
        api.driver.execute_script.assert_not_called()

    def test_wait_has_a_deadline(self):
        api = self.api()
        api.driver.execute_script.side_effect = ['', {'ready': False}]
        with patch('samokat.firefox.time.monotonic', side_effect=[0, 0, 2]), patch('samokat.firefox.time.sleep'), self.assertRaises(ParserError):
            api._wait_for_bridge(1)

    def test_native_guest_session_refresh_is_not_delayed_by_catalog_gate(self):
        api = self.api()
        request = Mock()
        request.url = 'https://api-web.samokat.ru/oauth/token/refresh'
        request._params = {'isBlocked': True}
        # Без condition/очереди: служебный запрос не должен входить в gate.
        api._before(request)
        request.continue_request.assert_called_once()
        request.fail.assert_not_called()

    def test_guest_session_requests_also_stop_after_block(self):
        api = self.api()
        api._block('HTTP 403')
        request = Mock()
        request.url = 'https://api-web.samokat.ru/oauth/token/refresh'
        request._params = {'isBlocked': True}
        api._before(request)
        request.continue_request.assert_not_called()
        request.fail.assert_called_once()


if __name__ == '__main__':
    unittest.main()
