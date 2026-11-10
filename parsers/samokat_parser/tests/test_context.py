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

    def test_null_webdriver_result_restores_and_preserves_category_request(self):
        api = self.api()
        url = '/v2/showcases/store/categories/bread'
        body = {'filters': {}, 'withPagination': False, 'limit': 100}
        node = {'uuid': 'bread', 'products': [], 'pageInfo': {'hasNextPage': False}}
        api.driver.execute_async_script.side_effect = [None, {'data': node}]
        with patch.object(api, '_wait_for_bridge') as restore:
            self.assertEqual(api.query(url, 'POST', body), node)
        restore.assert_called_once_with(60)
        requests = [call.args[1] for call in api.driver.execute_async_script.call_args_list]
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(requests[1]['body'], body)

    def test_repeated_null_output_stops_without_fabricating_empty_category(self):
        api = self.api()
        api.driver.execute_async_script.return_value = None
        with patch.object(api, '_wait_for_bridge') as restore, self.assertRaises(ParserError) as caught:
            api.query('/showcases/list')
        self.assertNotIsInstance(caught.exception, SchemaError)
        self.assertIn('3 попыток', str(caught.exception))
        self.assertEqual(restore.call_count, 2)
        self.assertEqual(api.driver.execute_async_script.call_count, 3)

    def test_null_recovery_preserves_block_conditions(self):
        for status in (403, 429):
            api = self.api()
            api.driver.execute_async_script.side_effect = [None, {'error': {'status': status}}]
            with self.subTest(status=status), patch.object(api, '_wait_for_bridge'), self.assertRaises(Blocked):
                api.query('/showcases/list')
            self.assertEqual(api.driver.execute_async_script.call_count, 2)
        api = self.api()
        api.driver.execute_async_script.return_value = None
        with patch.object(api, '_wait_for_bridge', side_effect=Blocked('captcha')), self.assertRaises(Blocked):
            api.query('/showcases/list')
        self.assertEqual(api.driver.execute_async_script.call_count, 1)

    def test_null_api_payload_is_distinct_from_null_webdriver_output(self):
        api = self.api()
        api.driver.execute_async_script.return_value = {'data': None}
        with patch.object(api, '_wait_for_bridge') as restore:
            self.assertIsNone(api.query('/showcases/list'))
        restore.assert_not_called()

    def test_other_malformed_envelopes_report_type_without_contents(self):
        for value in ([], 'private-data', 17, False):
            api = self.api()
            api.driver.execute_async_script.return_value = value
            with self.subTest(value=value), patch.object(api, '_wait_for_bridge') as restore, self.assertRaises(SchemaError) as caught:
                api.query('/showcases/list')
            restore.assert_not_called()
            self.assertIn(type(value).__name__, str(caught.exception))
            self.assertNotIn('private-data', str(caught.exception))

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
        api.driver.execute_script.side_effect = ['', {'ready': False}, 'https://samokat.ru/',
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
        api.driver.execute_script.side_effect = ['', {'ready': False}, 'https://samokat.ru/']
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

    def test_initial_blank_transitions_to_ready_site_instead_of_aborting(self):
        api = self.api()
        type(api.driver).current_url = unittest.mock.PropertyMock(side_effect=[
            'about:blank', 'about:blank',  # check(), _wait_for_bridge()
            'https://samokat.ru/', 'https://samokat.ru/',
            'https://samokat.ru/', 'https://samokat.ru/',
        ])
        api.driver.execute_script.side_effect = ['', {'ready': True, 'version': '2.86.1'}, 42,
                                                 '', {'ready': True, 'version': '2.86.1'}, 42]
        with patch('samokat.firefox.time.sleep'):
            api._wait_for_bridge(60)
        self.assertEqual(api.version, '2.86.1')
        scripts = [call.args[0] for call in api.driver.execute_script.call_args_list]
        self.assertEqual(len(scripts), 6)  # На about:blank адаптер не устанавливается.

    def test_blank_page_has_bounded_wait(self):
        api = self.api()
        api.driver.current_url = 'about:blank'
        with patch('samokat.firefox.time.monotonic', side_effect=[0, 0, 2]), patch('samokat.firefox.time.sleep'), self.assertRaises(ParserError) as caught:
            api._wait_for_bridge(1)
        self.assertIn('за отведённое время', str(caught.exception))
        api.driver.execute_script.assert_not_called()

    def test_browser_network_and_certificate_errors_are_precise_and_redacted(self):
        for page in ('neterror', 'certerror', 'blocked'):
            api = self.api()
            api.driver.current_url = f'about:{page}?e=nssFailure2&u=https://samokat.ru/?secret=hidden'
            with self.subTest(page=page), self.assertRaises(ParserError) as caught:
                api._wait_for_bridge(60)
            self.assertIn(f'about:{page}', str(caught.exception))
            self.assertIn('nssFailure2', str(caught.exception))
            self.assertNotIn('secret', str(caught.exception))
            api.driver.execute_script.assert_not_called()

    def test_hidden_network_error_document_is_detected(self):
        api = self.api()
        api.driver.execute_script.side_effect = ['', {'ready': False},
                                                 'about:neterror?e=dnsNotFound&u=https://samokat.ru/']
        with self.assertRaises(ParserError) as caught:
            api._wait_for_bridge(60)
        self.assertIn('dnsNotFound', str(caught.exception))

    def test_external_redirect_reports_only_origin_without_injection(self):
        api = self.api()
        api.driver.current_url = 'https://example.org/private?secret=hidden#token'
        with self.assertRaises(ParserError) as caught:
            api._wait_for_bridge(60)
        self.assertIn('https://example.org', str(caught.exception))
        self.assertNotIn('secret', str(caught.exception))
        self.assertNotIn('private', str(caught.exception))
        api.driver.execute_script.assert_not_called()

    def test_http_does_not_install_adapter_before_secure_redirect(self):
        api = self.api()
        api.driver.current_url = 'http://samokat.ru/'
        api.driver.execute_script.return_value = ''
        with self.assertRaises(ParserError):
            api._wait_for_bridge(60)
        # check() может прочитать текст страницы, но bridge.js не выполняется.
        self.assertEqual(api.driver.execute_script.call_count, 1)

    def test_sber_service_redirect_waits_without_injecting_adapter(self):
        api = self.api()
        type(api.driver).current_url = unittest.mock.PropertyMock(side_effect=[
            'https://id.sber.ru/', 'https://id.sber.ru/',
            'https://samokat.ru/', 'https://samokat.ru/',
            'https://samokat.ru/', 'https://samokat.ru/',
        ])
        api.driver.execute_script.side_effect = ['',  # Только видимый текст на id.sber.ru.
                                                 '', {'ready': True, 'version': '2.86.1'}, 42,
                                                 '', {'ready': True, 'version': '2.86.1'}, 42]
        with patch('samokat.firefox.time.sleep'):
            api._wait_for_bridge(60)
        self.assertEqual(api.version, '2.86.1')
        scripts = [call.args[0] for call in api.driver.execute_script.call_args_list]
        self.assertIn('innerText', scripts[0])
        self.assertNotIn('installBridge', scripts[0])

    def test_sber_redirect_timeout_does_not_attempt_login(self):
        api = self.api()
        api.driver.current_url = 'https://id.sber.ru/'
        with patch('samokat.firefox.time.monotonic', side_effect=[0, 0, 2]), patch('samokat.firefox.time.sleep'), self.assertRaises(ParserError) as caught:
            api._wait_for_bridge(1)
        self.assertIn('id.sber.ru', str(caught.exception))
        self.assertEqual(api.driver.execute_script.call_count, 1)
        self.assertIn('innerText', api.driver.execute_script.call_args.args[0])
        api.driver.execute_async_script.assert_not_called()

    def test_sber_captcha_is_still_a_stop_condition(self):
        api = self.api()
        api.driver.current_url = 'https://id.sber.ru/'
        api.driver.execute_script.return_value = 'Подтвердите, что вы не робот'
        with self.assertRaises(Blocked):
            api._wait_for_bridge(60)

    def test_sber_lookalike_domain_is_not_trusted(self):
        api = self.api()
        api.driver.current_url = 'https://id.sber.ru.example.org/'
        with self.assertRaises(ParserError):
            api._wait_for_bridge(60)
        api.driver.execute_script.assert_not_called()


if __name__ == '__main__':
    unittest.main()
