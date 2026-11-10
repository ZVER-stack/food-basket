"""Сохранённые публичные ответы: офлайн-восстановление и продолжение запросов."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from .core import ParserError, now_moscow

LOG = logging.getLogger(__name__)


class MissingCapture(ParserError):
    pass


def request_key(url, method='GET', body=None, params=None):
    request = {'url': url, 'method': method.upper()}
    if body is not None:
        request['body'] = body
    if params is not None:
        request['params'] = params
    return json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


class QuerySession:
    """Firefox запускается лишь при первом запросе, которого нет в записях.

    Время старой записи сохраняется. Новая запись не заменяет исходный JSON.
    offline=True гарантирует, что factory не вызывается ни при каких условиях.
    """
    def __init__(self, directory, factory=None, *, offline=False, reuse=True):
        self.directory = Path(directory)
        self.factory = factory
        self.offline = offline
        self.reuse = reuse or offline
        self.live = None
        self.records = {}
        self.hits = 0
        self.network_queries = 0
        self.last_observed_at = None
        self.last_from_capture = False
        if not offline:
            self.directory.mkdir(parents=True, exist_ok=True)
        if offline and not self.directory.is_dir():
            raise MissingCapture(f'Нет папки записей: {self.directory}')
        for path in sorted(self.directory.glob('*.json')):
            try:
                doc = json.loads(path.read_text(encoding='utf-8-sig'))
                req = doc['request']
                captured = doc['captured_at']
                datetime.strptime(captured, '%Y-%m-%d %H:%M:%S')
                key = request_key(req['url'], req.get('method', 'GET'), req.get('body'), req.get('params'))
                if 'data' not in doc:
                    raise ValueError('нет data')
                if key not in self.records or captured >= self.records[key]['captured_at']:
                    self.records[key] = doc
            except (ValueError, KeyError, TypeError):
                LOG.warning('Повреждённая/неподдерживаемая запись пропущена: %s', path.name)
        if self.records and self.reuse:
            stamps = [d['captured_at'] for d in self.records.values()]
            LOG.info('Найдены записи %s запросов, время по Москве: %s — %s; даты цен сохраняются',
                     len(self.records), min(stamps), max(stamps))

    def query(self, url, method='GET', body=None, params=None):
        key = request_key(url, method, body, params)
        if self.reuse and key in self.records:
            doc = self.records[key]
            self.hits += 1
            self.last_observed_at = doc['captured_at']
            self.last_from_capture = True
            return doc['data']
        if self.offline:
            raise MissingCapture(f'Нет сохранённого ответа: {method} {url}; нужен сетевой сбор этой части')
        if self.live is None:
            if self.factory is None:
                raise ParserError('Не задан браузерный транспорт')
            LOG.info('Для недостающих ответов запускается Firefox')
            self.live = self.factory()
        data = self.live.query(url, method, body, params)
        self.network_queries += 1
        stamp = now_moscow()
        self.last_observed_at = stamp
        self.last_from_capture = False
        req = json.loads(key)
        doc = {'request': req, 'data': data, 'captured_at': stamp}
        digest = hashlib.sha256(key.encode('utf-8')).hexdigest()[:20]
        suffix = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        path = self.directory / f'{digest}_{suffix}.json'
        temp = path.with_suffix('.json.tmp')
        with temp.open('w', encoding='utf-8') as f:
            json.dump(doc, f, ensure_ascii=False, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        self.records[key] = doc
        # Внутри этого прохода повтор того же запроса не нужен даже с --fresh-requests.
        if not self.reuse:
            self.reuse = True
            # Старые записи при --fresh-requests не должны стать доступными.
            self.records = {key: doc}
        return data

    def close(self):
        if self.live is not None:
            self.live.close()
            self.live = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
