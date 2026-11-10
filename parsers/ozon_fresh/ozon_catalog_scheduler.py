"""Coordinator-owned category cursors with bounded asynchronous page reads.

Only the consumer may validate a page, checkpoint it and release its continuation.
Independent categories can overlap; pages of one category never overlap.
"""
from collections import deque
from concurrent.futures import FIRST_COMPLETED, wait
from dataclasses import dataclass, field
import json
import threading
from urllib.parse import quote, urlsplit, urlunsplit

import ozon_fresh_fast as core


@dataclass
class CatalogTask:
    category: dict
    page_index: int
    url: str
    cached_page: object = None
    data: object = None
    captured_at: object = None
    page_key: str = ""
    _scheduler: object = field(default=None, repr=False)
    _chain: object = field(default=None, repr=False)

    def complete(self, next_url):
        """Release this category's next cursor after the caller checkpoints it."""
        return self._scheduler._complete_task(self, next_url)


class CatalogScheduler:
    def __init__(self, categories, state, cache_key, request_async, workers=4):
        if type(workers) is not int or workers < 1:
            raise ValueError("workers must be a positive integer")
        self.state, self.cache_key = state, cache_key
        self.request_async, self.workers = request_async, workers
        self._owner = threading.get_ident()
        self._ready = deque({"category": category, "url": category["url"],
                             "page_index": 0, "seen": set()} for category in categories)
        self._pending = []
        self._current = None
        self._failure = None
        self._closed = False

    def _check_owner(self):
        if threading.get_ident() != self._owner:
            raise core.ParseError("Category scheduler must run on the address coordinator thread")

    @staticmethod
    def _request_url(url):
        url = core.public_url(url)
        parts = urlsplit(url)
        if parts.path.startswith("/api/entrypoint-api."):
            return url, url
        if parts.path.startswith("/category/"):
            relative = urlunsplit(("", "", parts.path, parts.query, ""))
            return url, core.API + quote(relative, safe="")
        raise core.ParseError("Pagination URL is outside the catalog")

    def _fill(self):
        while self._ready and len(self._pending) < self.workers:
            chain = self._ready.popleft()
            url, request_url = self._request_url(chain["url"])
            if url in chain["seen"]:
                raise core.ParseError("Repeated pagination URL; catalog remains incomplete")
            chain["seen"].add(url)
            chain["page_index"] += 1
            page_key = json.dumps([self.cache_key, chain["category"]["category_id"]])
            cached = self.state.get_page(page_key, url) if self.state else None
            task = CatalogTask(chain["category"], chain["page_index"], url,
                               cached_page=cached, page_key=page_key,
                               _scheduler=self, _chain=chain)
            if cached is not None:
                task.captured_at = cached[0]
                future = None
            else:
                future = self.request_async({"url": request_url})
            self._pending.append((task, future))

    def __iter__(self):
        return self

    def __next__(self):
        self._check_owner()
        if self._current is not None:
            raise core.ParseError("Previous category page must be completed before requesting another")
        if self._closed:
            raise StopIteration
        if self._failure is not None:
            raise self._failure
        try:
            self._fill()
            if not self._pending:
                raise StopIteration
            ready = next((i for i, (_, future) in enumerate(self._pending)
                          if future is None or future.done()), None)
            if ready is None:
                wait([future for _, future in self._pending], return_when=FIRST_COMPLETED)
                ready = next(i for i, (_, future) in enumerate(self._pending) if future.done())
            task, future = self._pending.pop(ready)
            if future is not None:
                result = future.result()
                task.data = result["data"]
                task.captured_at = result["captured_at"]
            self._current = task
            return task
        except StopIteration:
            raise
        except BaseException as exc:
            self._failure = exc
            raise

    def _complete_task(self, task, next_url):
        self._check_owner()
        if self._closed or task is not self._current:
            raise core.ParseError("Category page is no longer awaiting completion")
        if next_url not in (None, ""):
            if not isinstance(next_url, str):
                raise core.ParseError("Invalid next-page link")
            url, _ = self._request_url(next_url)
            if url in task._chain["seen"]:
                raise core.ParseError("Repeated pagination URL; catalog remains incomplete")
            task._chain["url"] = url
            if self.workers==1:self._ready.appendleft(task._chain)
            else:self._ready.append(task._chain)
        self._current = None

    def complete(self, next_url):
        self._check_owner()
        if self._current is None:
            raise core.ParseError("No category page is awaiting completion")
        self._complete_task(self._current, next_url)

    def drain(self):
        """Join every submitted read before the caller may change the address.

        This never starts new reads or cancels work. The caller owns stop events.
        All futures are joined even if an earlier future failed.
        """
        self._check_owner()
        self._closed = True
        failure = self._failure
        pending, self._pending = self._pending, []
        for _, future in pending:
            if future is None:
                continue
            try:
                future.result()
            except BaseException as exc:
                if failure is None:
                    failure = exc
        self._ready.clear()
        self._failure = failure
        if failure is not None:
            raise failure

    close = drain

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.drain()
        return False
