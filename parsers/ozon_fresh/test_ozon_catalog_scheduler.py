"""Offline cursor scheduling, address barriers and coordinator ownership."""
from concurrent.futures import Future, ThreadPoolExecutor
import json
import threading
import time
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

import ozon_fresh_fast as core
from ozon_catalog_scheduler import CatalogScheduler


def categories(count):
    return [{"category_id": str(i), "url": f"/category/c-{i}/"} for i in range(count)]


class CoordinatorState:
    def __init__(self, pages=None):
        self.pages = pages or {}
        self.owner = threading.get_ident()
        self.calls = []

    def get_page(self, key, url):
        if threading.get_ident() != self.owner:
            raise AssertionError("SQLite accessed from worker")
        self.calls.append((key, url))
        return self.pages.get((key, url))


class SchedulerTests(unittest.TestCase):
    def test_round_robin_real_cursors_all_pages_once_and_bounded_outstanding(self):
        state = CoordinatorState()
        submitted, futures = [], []
        def request(spec):
            self.assertEqual(threading.get_ident(), state.owner)
            submitted.append(spec)
            f = Future(); futures.append(f)
            return f
        scheduler = CatalogScheduler(categories(6), state, "house", request, workers=4)
        iterator_result = []
        # Resolve the fourth submitted page first; iterator must not wait for first.
        def resolve():
            while len(futures) < 4:
                time.sleep(.001)
            futures[3].set_result({"data": {"head": 3}, "captured_at": "t3"})
        thread = threading.Thread(target=resolve); thread.start()
        task = next(scheduler); thread.join()
        self.assertEqual(task.category["category_id"], "3")
        self.assertEqual(len(submitted), 4)
        self.assertEqual(len(scheduler._pending), 3)
        cursor = "/category/c-3/?cursor=opaque%2Fvalue&page=99"
        task.complete(cursor)
        self.assertEqual(len(submitted), 4)  # complete never submits reads.
        iterator_result.append(task.url)
        # Make futures immediately available from here, retaining the initial four.
        for f in futures[:3]:
            f.set_result({"data": {}, "captured_at": "t"})
        original = scheduler.request_async
        def immediate(spec):
            f = original(spec); f.set_result({"data": {}, "captured_at": "t"}); return f
        scheduler.request_async = immediate
        for task in scheduler:
            self.assertLessEqual(len(scheduler._pending), 3)
            iterator_result.append(task.url)
            task.complete(None)
        self.assertEqual(len(iterator_result), 7)
        self.assertEqual(len(set(iterator_result)), 7)
        requested_paths = [parse_qs(urlsplit(s["url"]).query)["url"][0] for s in submitted]
        self.assertIn(cursor, requested_paths)
        self.assertNotIn("/category/c-3/?page=2", requested_paths)
        self.assertEqual([urlsplit(u).path for u in requested_paths[:6]],
                         [f"/category/c-{i}/" for i in range(6)])
        scheduler.close()

    def test_cache_is_read_only_on_coordinator_and_preserved(self):
        url = core.public_url("/category/c-0/")
        cached = ("2026-10-18 10:00:00", {"rows": [{"product_id": "x"}], "next": None})
        key = json.dumps(["h", "0"])
        state = CoordinatorState({(key, url): cached})
        def never(spec):
            self.fail("Cached page must not make a request")
        scheduler = CatalogScheduler(categories(1), state, "h", never)
        task = next(scheduler)
        self.assertIs(task.cached_page, cached)
        self.assertEqual(task.captured_at, cached[0])
        self.assertIsNone(task.data)
        self.assertEqual(task.page_key, key)
        scheduler.complete(task.cached_page[1]["next"])
        self.assertEqual(list(scheduler), [])
        scheduler.drain()

    def test_missing_completion_double_completion_and_repeated_url_rejected(self):
        def request(spec):
            f = Future(); f.set_result({"data": {}, "captured_at": "t"}); return f
        scheduler = CatalogScheduler(categories(1), None, "h", request, workers=1)
        task = next(scheduler)
        with self.assertRaisesRegex(core.ParseError, "must be completed"):
            next(scheduler)
        with self.assertRaisesRegex(core.ParseError, "Repeated pagination"):
            task.complete(task.url)
        task.complete("/category/c-0/?cursor=second")
        with self.assertRaisesRegex(core.ParseError, "no longer"):
            task.complete(None)
        second = next(scheduler)
        self.assertEqual(second.page_index, 2)
        with self.assertRaisesRegex(core.ParseError, "Repeated pagination"):
            second.complete("/category/c-0/")
        second.complete(None)
        self.assertEqual(list(scheduler), [])

    def test_invalid_paths_fail_before_submission(self):
        for url in ("https://evil.example/category/a/", "/product/item/", "/category/a/#x"):
            with self.subTest(url=url):
                called = []
                scheduler = CatalogScheduler([{"category_id": "1", "url": url}], None,
                                             "h", lambda spec: called.append(spec))
                with self.assertRaises(core.ParseError):
                    next(scheduler)
                self.assertEqual(called, [])

    def test_entrypoint_url_is_not_double_encoded(self):
        url = core.API + "%2Fcategory%2Fx%2F%3Fcursor%3Da%252Fb"
        seen = []
        def request(spec):
            seen.append(spec["url"])
            f = Future(); f.set_result({"data": {}, "captured_at": "t"}); return f
        scheduler = CatalogScheduler([{"category_id": "x", "url": url}], None, "h", request)
        next(scheduler).complete(None)
        self.assertEqual(seen, [url])
        scheduler.close()

    def test_empty_categories_and_coordinator_ownership(self):
        scheduler = CatalogScheduler([], None, "h", lambda spec: self.fail("request"))
        self.assertEqual(list(scheduler), [])
        failures = []
        def foreign_thread():
            try:
                scheduler.complete(None)
            except Exception as exc:
                failures.append(exc)
        thread = threading.Thread(target=foreign_thread); thread.start(); thread.join()
        self.assertIsInstance(failures[0], core.ParseError)
        scheduler.close()

    def test_drain_waits_all_workers_even_after_failure_before_address_switch(self):
        release = threading.Event()
        finished = []
        pool = ThreadPoolExecutor(max_workers=4)
        self.addCleanup(pool.shutdown)
        def reader(spec):
            page = parse_qs(urlsplit(spec["url"]).query)["url"][0]
            if "c-0/" in page:
                raise core.ParseError("first read failed")
            release.wait(3)
            finished.append(page)
            return {"data": {}, "captured_at": "t"}
        scheduler = CatalogScheduler(categories(4), None, "h", lambda s: pool.submit(reader, s))
        with self.assertRaisesRegex(core.ParseError, "first read failed"):
            next(scheduler)
        with self.assertRaisesRegex(core.ParseError, "No category page"):
            scheduler.complete(None)
        timer = threading.Timer(.03, release.set); timer.start()
        try:
            with self.assertRaisesRegex(core.ParseError, "first read failed"):
                scheduler.drain()
        finally:
            release.set(); timer.join()
        self.assertEqual(len(finished), 3)
        self.assertEqual(list(scheduler), [])

    def test_sequential_immediate_future_preserves_chain(self):
        def request(spec):
            f = Future(); f.set_result({"data": {}, "captured_at": "t"}); return f
        scheduler = CatalogScheduler(categories(2), None, "h", request, workers=1)
        seen = []
        for task in scheduler:
            seen.append((task.category["category_id"], task.page_index))
            task.complete(task.url + "?cursor=next" if task.page_index == 1 else None)
        self.assertEqual(seen, [("0", 1), ("0", 2), ("1", 1), ("1", 2)])
        scheduler.close()


if __name__ == "__main__":
    unittest.main()
