"""Firefox + WebDriver BiDi. Не извлекает токены и не меняет защиту браузера."""
from __future__ import annotations

import logging
import heapq
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .core import Blocked, ParserError, SchemaError

LOG = logging.getLogger(__name__)


def cached_geckodriver(cache_root=None):
    """Локальный драйвер из стандартного кэша Selenium; сеть не используется."""
    if os.name != 'nt':
        return None
    root = Path(cache_root) if cache_root is not None else Path(os.environ.get('SE_CACHE_PATH') or Path.home() / '.cache/selenium')
    candidates = []
    try:
        for path in (root / 'geckodriver/win64').glob('*/geckodriver.exe'):
            if path.is_file() and re.fullmatch(r'\d+\.\d+\.\d+', path.parent.name):
                candidates.append((tuple(int(n) for n in path.parent.name.split('.')), path))
    except OSError:
        return None
    return str(max(candidates)[1]) if candidates else None


class FirefoxAPI:
    def __init__(self, binary=None, geckodriver=None, interval=3.0, startup_timeout=240, headless=False, capture_dir=None,
                 use_cached_driver=True):
        if interval < 1:
            raise ValueError('Интервал запросов должен быть не меньше секунды')
        self.interval = interval
        self.startup_timeout = startup_timeout
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.waiters = []
        self.ticket = 0
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
        if not geckodriver and use_cached_driver:
            geckodriver = cached_geckodriver()
            if geckodriver:
                LOG.info('Используется geckodriver из кэша: %s', geckodriver)
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
            self._wait_for_bridge(startup_timeout)
            self.check()
            LOG.info('Firefox %s; публичный web-клиент %s', self.driver.capabilities['browserVersion'], self.version)
        except BaseException:
            self.close()
            raise

    def _block(self, reason):
        self.reason = reason
        self.stopped.set()

    @staticmethod
    def _navigation_error(error):
        # Только ошибки смены документа. Ошибки JS адаптера не скрываем.
        from selenium.common.exceptions import WebDriverException
        return isinstance(error, WebDriverException) and any(phrase in str(error).lower() for phrase in (
            'document was unloaded', 'execution context was destroyed',
            'browsing context has been discarded', "can't access dead object",
        ))

    @staticmethod
    def _page_kind(current):
        parsed = urlsplit(current)
        if not current or (parsed.scheme == 'about' and parsed.path in ('blank', 'home', 'newtab')):
            return 'loading'
        if parsed.scheme == 'about' and parsed.path in ('neterror', 'certerror', 'blocked'):
            # Не печатаем query: в нём может находиться полный адрес перехода.
            code = parse_qs(parsed.query).get('e', [''])[0]
            safe_code = ''.join(c for c in code if c.isascii() and (c.isalnum() or c in '_-'))[:60]
            label = f'about:{parsed.path}' + (f', код {safe_code}' if safe_code else '')
            raise ParserError(f'Firefox не смог открыть Самокат ({label}). Это страница ошибки браузера, а не смена сайта.')
        if parsed.scheme == 'https' and parsed.hostname in ('samokat.ru', 'www.samokat.ru'):
            return 'site'
        if parsed.scheme == 'https' and parsed.hostname == 'id.sber.ru':
            # Наблюдаемый служебный переход самого сайта. Только ждём возврата:
            # не внедряем адаптер, не нажимаем кнопки, не вводим данные.
            return 'auth_redirect'
        # Показываем только схему и хост, без пути, query и fragment.
        origin = f'{parsed.scheme}://{parsed.hostname}' if parsed.hostname else f'{parsed.scheme}:{parsed.path.split("/")[0]}'
        raise ParserError(f'Firefox открыл {origin}; ожидался https://samokat.ru. Адаптер на посторонней странице не запускается.')

    def _wait_for_bridge(self, timeout):
        bridge = Path(__file__).with_name('bridge.js').read_text(encoding='utf-8-sig')
        deadline = time.monotonic() + timeout
        previous_document = None
        navigation_phase = None
        while time.monotonic() < deadline:
            try:
                self.check()
                current = self.driver.current_url
                kind = self._page_kind(current)
                if kind in ('loading', 'auth_redirect'):
                    if navigation_phase != kind:
                        if kind == 'loading':
                            LOG.info('Firefox пока показывает стартовую страницу; ожидается загрузка samokat.ru')
                        else:
                            LOG.info('Firefox перешёл на id.sber.ru; ожидается автоматический возврат на samokat.ru')
                    navigation_phase = kind
                    previous_document = None
                    state = {}
                else:
                    navigation_phase = None
                    state = self.driver.execute_script(bridge) or {}
                if not isinstance(state, dict):
                    raise SchemaError('Неизвестный ответ адаптера страницы')
                if state.get('error'):
                    raise SchemaError(state['error'])
                if state.get('ready'):
                    # Два наблюдения одного документа, не ожидание пустой API-очереди.
                    document = self.driver.execute_script('return performance.timeOrigin')
                    if document == previous_document and previous_document is not None:
                        self.version = state.get('version')
                        return
                    previous_document = document
                else:
                    previous_document = None
                    if kind == 'site':
                        # На странице ошибки current_url иногда остаётся адресом
                        # назначения. documentURI позволяет отличить neterror.
                        document_uri = self.driver.execute_script('return document.documentURI')
                        if isinstance(document_uri, str) and document_uri.startswith('about:'):
                            self._page_kind(document_uri)
            except Exception as error:
                if not self._navigation_error(error):
                    raise
                previous_document = None
            time.sleep(0.5)
        if navigation_phase == 'auth_redirect':
            raise ParserError('Служебный переход через id.sber.ru не вернулся на samokat.ru за отведённое время. Вход в аккаунт не выполнялся.')
        raise ParserError('Гостевая витрина не стала готова после загрузки/смены страницы за отведённое время')

    def _before(self, request):
        # Событие BiDi приходит также для запросов, не попавших в intercept,
        # например для preflight. Их нельзя продолжать повторно.
        if not request._params.get('isBlocked', False):
            return
        path = urlsplit(request.url).path
        # Нативные служебные запросы сайта работают в обычном режиме браузера.
        # Искусственная очередь для feature-config/сессии/платежей тормозила
        # и ломала интерфейс. Ограничение остаётся на запросах каталога.
        is_catalog = path.startswith(('/v2/showcases/', '/showcases/')) or path == '/delivery/info'
        if not is_catalog:
            try:
                if self.stopped.is_set():
                    request.fail()
                else:
                    request.continue_request()
            except Exception as error:
                if 'no such request' in str(error).lower():
                    # При quit()/смене страницы браузер может удалить запрос
                    # до прихода callback. Отменять его повторно уже не нужно.
                    LOG.debug('Служебный запрос уже отменён браузером')
                elif self.stopped.is_set():
                    LOG.warning('Не удалось завершить служебный запрос при остановке: %s', type(error).__name__)
                else:
                    self._block('Не удалось продолжить служебный запрос сайта')
            return
        priority = 0
        with self.condition:
            self.pending += 1
            self.ticket += 1
            item = (priority, self.ticket)
            heapq.heappush(self.waiters, item)
            try:
                while not self.stopped.is_set():
                    remaining = self.interval - (time.monotonic() - self.last_request)
                    if self.waiters[0] == item and remaining <= 0:
                        # Не более одной отправки каталога за заданный интервал.
                        request.continue_request()
                        self.last_request = time.monotonic()
                        break
                    self.condition.wait(timeout=max(0.01, min(max(remaining, 0.01), 0.25)))
                else:
                    request.fail()
            except Exception as e:
                if 'no such request' in str(e).lower():
                    LOG.debug('Запрос отменён браузером при переходе страницы')
                else:
                    self._block(f'Не удалось контролировать частоту запросов: {type(e).__name__}')
            finally:
                self.waiters.remove(item)
                heapq.heapify(self.waiters)
                self.pending -= 1
                self.condition.notify_all()

    def _response(self, event):
        response = event.get('response', {})
        status = response.get('status')
        url = urlsplit(response.get('url', ''))
        if url.scheme not in ('http', 'https'):
            return
        safe_url = f'{url.scheme}://{url.netloc}{url.path}'
        LOG.debug('HTTP %s %s', status, safe_url)
        if status == 401 and url.hostname in ('api-web.samokat.ru', 'web.samokat.io', 'api.samokat.ru'):
            LOG.warning('HTTP 401: %s; гостевую сессию обновляет сайт', safe_url)
        if status in (403, 429):
            self._block(f'HTTP {status}: {safe_url}. Сбор остановлен; увеличьте --interval перед новым запуском.')

    def check(self):
        if self.stopped.is_set():
            raise Blocked(self.reason or 'Сбор остановлен')
        if self.driver and urlsplit(self.driver.current_url).hostname in ('samokat.ru', 'www.samokat.ru', 'id.sber.ru'):
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
        query = {'url': url, 'method': method, 'timeout': 150000}
        if body is not None:
            query['body'] = body
        if params is not None:
            query['params'] = params
        for attempt in range(3):
            try:
                self.check()
                result = self.driver.execute_async_script('''
                    const done=arguments[arguments.length-1];
                    if (!window.__samokatParser) return done({bridgeState:'missing'});
                    window.__samokatParser.query(arguments[0])
                        .then(done).catch(e=>done({bridgeError:String(e)}));
                ''', query)
                self.check()
            except Exception as error:
                if not self._navigation_error(error):
                    raise
                result = {'bridgeState': 'document_unloaded'}
            # The adapter always returns an envelope (even for null data).
            # A null WebDriver result is missing transport output, not a product schema.
            # Recheck the guest document and share the existing bounded retry budget.
            if result is None:
                result = {'bridgeState': 'empty_result'}
            if isinstance(result, dict) and result.get('bridgeState') in ('missing', 'not_ready', 'document_unloaded', 'empty_result'):
                if attempt == 2:
                    if result.get('bridgeState') == 'empty_result':
                        raise ParserError(f'{method} {url}: Firefox не вернул результат адаптера после 3 попыток; зона не завершена')
                    raise ParserError('Страница повторно меняет контекст; предел восстановления достигнут. Данные сохранены.')
                if result.get('bridgeState') == 'empty_result':
                    LOG.warning('Firefox не вернул результат адаптера; восстановление %s/2: %s', attempt + 1, url)
                else:
                    LOG.warning('Страница сменила контекст; восстановление гостевого адаптера %s/2: %s', attempt + 1, url)
                self._wait_for_bridge(min(getattr(self, 'startup_timeout', 240), 60))
                continue
            error = result.get('error') if isinstance(result, dict) else None
            status = error.get('originalStatus', error.get('status')) if isinstance(error, dict) else None
            if status not in ('FETCH_ERROR', 'TIMEOUT_ERROR', 500, 502, 503, 504) or attempt == 2:
                break
            LOG.warning('Временная ошибка %s; повтор %s/2: %s', status, attempt + 1, url)
            time.sleep(2 ** (attempt + 1))
            self.check()
        if not isinstance(result, dict):
            raise SchemaError(f'{url}: ответ браузерного клиента не является объектом (тип: {type(result).__name__})')
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
