"""Firefox + WebDriver BiDi. Не извлекает токены и не меняет защиту браузера."""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .core import Blocked, ParserError, SchemaError

LOG = logging.getLogger(__name__)


class FirefoxAPI:
    def __init__(self, binary=None, geckodriver=None, interval=3.0, startup_timeout=240, headless=False, capture_dir=None):
        if interval < 1:
            raise ValueError('Интервал запросов должен быть не меньше секунды')
        self.interval = interval
        self.startup_timeout = startup_timeout
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.last_request = 0.0
        self.pending = 0
        self.reason = ''
        self.capture_dir = Path(capture_dir) if capture_dir else None
        if self.capture_dir:
            self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.driver = None
        self.handlers = []
        from selenium import webdriver
        from selenium.webdriver.firefox.options import Options
        from selenium.webdriver.firefox.service import Service
        options = Options()
        options.accept_insecure_certs = False
        options.enable_bidi = True
        options.page_load_strategy = 'eager'
        # Новый временный профиль: личные куки и аккаунты не используются.
        if binary:
            options.binary_location = str(Path(binary).resolve())
        elif os.name == 'nt' and not Path(r'C:\Program Files\Mozilla Firefox\firefox.exe').exists():
            packages = Path(r'C:\Program Files\WindowsApps')
            try:
                candidates = sorted(packages.glob('Mozilla.Firefox_*/VFS/ProgramFiles/*/firefox.exe'), reverse=True)
                if candidates:
                    options.binary_location = str(candidates[0])
            except OSError:
                pass
        if headless:
            options.add_argument('-headless')
        service = Service(executable_path=str(geckodriver)) if geckodriver else Service()
        try:
            self.driver = webdriver.Firefox(options=options, service=service)
            self.driver.set_page_load_timeout(startup_timeout)
            self.driver.set_script_timeout(180)
            self.network = self.driver.network
            self.handlers.append(('request', self.network.add_request_handler(
                'before_request', self._before, url_patterns=[
                    {'type': 'pattern', 'hostname': host}
                    for host in ('api-web.samokat.ru', 'web.samokat.io', 'api.samokat.ru')
                ])))
            self.handlers.append(('response_started', self.network.add_event_handler('response_started', self._response)))
            self.driver.get('https://samokat.ru/')
            bridge = Path(__file__).with_name('bridge.js').read_text(encoding='utf-8-sig')
            deadline = time.monotonic() + startup_timeout
            ready = False
            while time.monotonic() < deadline:
                self.check()
                if urlsplit(self.driver.current_url).hostname in ('samokat.ru', 'www.samokat.ru'):
                    state = self.driver.execute_script(bridge) or {}
                    if state.get('error'):
                        raise SchemaError(state['error'])
                    if state.get('ready'):
                        ready = True
                        self.version = state.get('version')
                        # Дождаться исходной очереди загрузки, чтобы таймаут API не
                        # расходовался на ожидание скриптов и картинок сайта.
                        if self.pending == 0 and time.monotonic() - self.last_request >= 3:
                            break
                time.sleep(0.5)
            else:
                if not ready or self.pending:
                    raise ParserError('Firefox не завершил загрузку анонимной витрины за отведённое время')
            self.check()
            LOG.info('Firefox %s; публичный web-клиент %s', self.driver.capabilities['browserVersion'], self.version)
        except BaseException:
            self.close()
            raise

    def _block(self, reason):
        self.reason = reason
        self.stopped.set()

    def _before(self, request):
        # Событие BiDi приходит также для запросов, не попавших в intercept,
        # например для preflight. Их нельзя продолжать повторно.
        if not request._params.get('isBlocked', False):
            return
        self.pending += 1
        try:
            with self.lock:
                if not self.stopped.is_set():
                    self.stopped.wait(max(0, self.interval - (time.monotonic() - self.last_request)))
                if self.stopped.is_set():
                    request.fail()
                else:
                    # Продолжение внутри lock: ограничиваем реальные отправки API,
                    # а не только число вызовов метода query из Python.
                    request.continue_request()
                    self.last_request = time.monotonic()
        except Exception as e:
            if 'no such request' in str(e).lower():
                LOG.debug('Запрос отменён браузером при переходе страницы')
            else:
                self._block(f'Не удалось контролировать частоту запросов: {type(e).__name__}')
        finally:
            self.pending -= 1

    def _response(self, event):
        response = event.get('response', {})
        status = response.get('status')
        url = urlsplit(response.get('url', ''))
        if url.scheme not in ('http', 'https'):
            return
        safe_url = f'{url.scheme}://{url.netloc}{url.path}'
        LOG.debug('HTTP %s %s', status, safe_url)
        if status in (403, 429):
            self._block(f'HTTP {status}: {safe_url}. Сбор остановлен; увеличьте --interval перед новым запуском.')

    def check(self):
        if self.stopped.is_set():
            raise Blocked(self.reason or 'Сбор остановлен')
        if self.driver and urlsplit(self.driver.current_url).hostname in ('samokat.ru', 'www.samokat.ru'):
            # Проверяем текст видимой страницы, а не слова в JS-коде сайта.
            value = self.driver.execute_script('return (document.body?.innerText || "").slice(0,12000)') or ''
            lowered = value.lower()
            phrases = ('подтвердите, что вы не робот', 'подтвердите, что вы человек',
                       'пройдите проверку', 'доступ ограничен', 'access denied', 'too many requests',
                       'verify you are human', 'проверка браузера', 'captcha')
            if any(s in lowered for s in phrases):
                self._block('Капча или страница ограничения доступа. Автоматический сбор остановлен.')
                raise Blocked(self.reason)

    def query(self, url, method='GET', body=None, params=None):
        # Только явно перечисленные read-only операции публичной витрины.
        import re
        allowed = (
            r'/showcases/list', r'/showcases/[^/]+/settings', r'/delivery/info',
            r'/v2/showcases/[^/]+/categories/list',
            r'/v2/showcases/[^/]+/main',
            r'/v2/showcases/[^/]+/categories/[^/]+',
            r'/v2/showcases/[^/]+/categories/[^/]+/products',
            r'/v2/showcases/[^/]+/products/[^/]+',
        )
        if not any(re.fullmatch(p, url) for p in allowed) or method not in ('GET', 'POST'):
            raise ParserError(f'Запрос вне публичного каталога запрещён: {method} {url}')
        self.check()
        query = {'url': url, 'method': method, 'timeout': 150000}
        if body is not None:
            query['body'] = body
        if params is not None:
            query['params'] = params
        for attempt in range(3):
            result = self.driver.execute_async_script('''
                const done=arguments[arguments.length-1];
                if (!window.__samokatParser) return done({bridgeError:'Контекст сайта изменился'});
                window.__samokatParser.query(arguments[0])
                    .then(done).catch(e=>done({bridgeError:String(e)}));
            ''', query)
            self.check()
            error = result.get('error') if isinstance(result, dict) else None
            status = error.get('originalStatus', error.get('status')) if isinstance(error, dict) else None
            if status not in ('FETCH_ERROR', 'TIMEOUT_ERROR', 500, 502, 503, 504) or attempt == 2:
                break
            LOG.warning('Временная ошибка %s; повтор %s/2: %s', status, attempt + 1, url)
            time.sleep(2 ** (attempt + 1))
            self.check()
        if not isinstance(result, dict):
            raise SchemaError(f'{url}: ответ браузерного клиента не является объектом')
        if result.get('bridgeError'):
            raise SchemaError(result['bridgeError'])
        if result.get('error'):
            error = result['error']
            status = error.get('originalStatus', error.get('status')) if isinstance(error, dict) else 'unknown'
            if status in (403, 429):
                self._block(f'HTTP {status}: {url}')
                raise Blocked(self.reason)
            # Не выводим сырые ответы об ошибках: в них могут оказаться служебные данные.
            raise ParserError(f'{method} {url}: ошибка API ({status}); зона не сохранена')
        if 'data' not in result:
            raise SchemaError(f'{url}: нет data в ответе клиента')
        if self.capture_dir:
            import hashlib
            import json
            from .core import now_moscow
            key = hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()[:20]
            (self.capture_dir / f'{key}.json').write_text(json.dumps({
                'request': query, 'data': result['data'], 'captured_at': now_moscow()
            }, ensure_ascii=False), encoding='utf-8')
        return result['data']

    def close(self):
        self.stopped.set()
        if self.driver:
            # Дать задержанным callbacks закончиться до закрытия WebSocket.
            deadline = time.monotonic() + 10
            while self.pending and time.monotonic() < deadline:
                time.sleep(0.05)
            for event, handler in self.handlers:
                try:
                    if event == 'request':
                        self.network.remove_request_handler('before_request', handler)
                    else:
                        self.network.remove_event_handler(event, handler)
                except Exception:
                    pass
            try:
                self.driver.quit()
            finally:
                self.driver = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
