"""Local-only receiver for product cards extracted from browser catalog pages.

This helper is intentionally bound to 127.0.0.1 and protected by a per-run
token. It merges observations by (service, product_id) into JSON snapshots.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SERVICE_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
MAX_BODY_BYTES = 5_000_000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def merge_products(output_dir: Path, service: str, observation: dict) -> tuple[int, int]:
    path = output_dir / f"{service}.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    by_id = {str(item["product_id"]): item for item in existing}
    added = 0
    updated = 0
    for raw in observation["products"]:
        product_id = str(raw.get("product_id") or "").strip()
        name = re.sub(r"\s+", " ", str(raw.get("name") or "")).strip()
        url = str(raw.get("url") or "").strip()
        if not product_id or not name or not url:
            continue
        clean = {
            "service": service,
            "product_id": product_id,
            "name": name,
            "brand": str(raw.get("brand") or "").strip() or None,
            "quantity_value": raw.get("quantity_value"),
            "quantity_unit": str(raw.get("quantity_unit") or "").strip() or None,
            "price": raw.get("price"),
            "currency": str(raw.get("currency") or "RUB"),
            "available": raw.get("available", True),
            "url": url,
            "category_hint": str(observation.get("category_hint") or "").strip(),
            "observed_queries": [str(observation.get("query") or "").strip()],
            "observed_page_urls": [str(observation.get("page_url") or "").strip()],
            "captured_at": now_iso(),
        }
        if product_id in by_id:
            previous = by_id[product_id]
            clean["observed_queries"] = sorted(
                {value for value in previous.get("observed_queries", []) + clean["observed_queries"] if value}
            )
            clean["observed_page_urls"] = sorted(
                {value for value in previous.get("observed_page_urls", []) + clean["observed_page_urls"] if value}
            )
            clean["first_captured_at"] = previous.get(
                "first_captured_at", previous.get("captured_at", clean["captured_at"])
            )
            by_id[product_id] = clean
            updated += 1
        else:
            clean["first_captured_at"] = clean["captured_at"]
            by_id[product_id] = clean
            added += 1
    output_dir.mkdir(parents=True, exist_ok=True)
    ordered = sorted(by_id.values(), key=lambda item: (item["name"].casefold(), item["product_id"]))
    path.write_text(json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8")
    return added, updated


def make_handler(output_dir: Path, token: str):
    class SinkHandler(BaseHTTPRequestHandler):
        server_version = "LocalCatalogSink/0.1"

        def log_message(self, format_string: str, *args: object) -> None:
            print(f"[{self.log_date_time_string()}] {format_string % args}", flush=True)

        def send_json(self, value: object, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self.send_json({"status": "ok"})
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/ingest":
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            if self.headers.get("Authorization") != f"Bearer {token}":
                self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BODY_BYTES:
                    raise ValueError("invalid request size")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                service = str(payload.get("service") or "")
                if not SERVICE_RE.fullmatch(service):
                    raise ValueError("invalid service")
                if not isinstance(payload.get("products"), list):
                    raise ValueError("products must be a list")
                added, updated = merge_products(output_dir, service, payload)
                self.send_json({"saved": True, "added": added, "updated": updated})
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    return SinkHandler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output-dir", type=Path, default=Path("data/service_snapshots"))
    parser.add_argument("--token", default=None)
    args = parser.parse_args()
    token = args.token or secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.output_dir.resolve(), token))
    print(f"Catalog sink: http://127.0.0.1:{args.port}", flush=True)
    print(f"Token: {token}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
