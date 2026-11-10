from __future__ import annotations

import html
import json
import re
import secrets
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.cookiejar import CookieJar
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener


SERVICE_CODE = "yandex_lavka"
BASE_URL = "https://lavka.yandex.ru"
DEFAULT_TIMEOUT_SECONDS = 20.0
ALLOWED_DEPOT_TYPES = ("regular", "supermarket", "cafe")
GROCERY_DEPOT_TYPES = ("regular", "supermarket")
STOREFRONT_NAMES = {
    "regular": "Лавка у дома",
    "supermarket": "Большая Лавка",
    "cafe": "Кафе",
}


class LavkaError(RuntimeError):
    """A normalized upstream error safe for callers to handle."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.status_code is not None:
            result["status_code"] = self.status_code
        if self.retry_after is not None:
            result["retry_after"] = self.retry_after
        if self.details:
            result["details"] = self.details
        return result


@dataclass(frozen=True)
class ResolvedLocation:
    city: str
    street: str
    house: str
    longitude: float
    latitude: float
    building_id: str | None = None
    depot_type: str = "supermarket"
    depot_id: str | None = None
    currency: str = "RUB"
    currency_sign: str = "₽"
    service_info: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def position(self) -> dict[str, list[float]]:
        # Lavka uses [longitude, latitude], not the more common [latitude, longitude].
        return {"location": [self.longitude, self.latitude]}

    @property
    def additional_data(self) -> dict[str, str]:
        return {"city": self.city, "street": self.street, "house": self.house}


class LavkaClient:
    """Small HTTP client for the endpoints used by the public Lavka web app."""

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        city_id: str = "213",
        before_request: Callable[[], None] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.city_id = city_id
        self._before_request = before_request
        self._cookies = CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(self._cookies))
        self._csrf_token = ""
        self._csrf_token_bff = ""
        self._bootstrap()

    def _bootstrap(self) -> None:
        html = self._request_text("/", method="GET", include_csrf=False)
        self._csrf_token = self._extract_page_token(html, "authProxyCsrfToken")
        self._csrf_token_bff = self._extract_page_token(html, "csrfToken")
        if not self._csrf_token or not self._csrf_token_bff:
            raise LavkaError(
                "UPSTREAM_CHANGED",
                "Lavka did not expose the expected guest CSRF tokens.",
            )

    @staticmethod
    def _extract_page_token(html: str, key: str) -> str:
        match = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', html)
        if not match:
            return ""
        try:
            return json.loads(f'"{match.group(1)}"')
        except json.JSONDecodeError:
            return ""

    def _headers(self, *, referer: str | None = None) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Content-Type": "application/json",
            "Origin": self.base_url,
            "Referer": referer or f"{self.base_url}/",
            "User-Agent": "Mozilla/5.0 (compatible; basket-comparison-research/0.1)",
            "X-Csrf-Token": self._csrf_token,
            "X-Csrf-Token-Bff": self._csrf_token_bff,
            "X-Lavka-Web-City": self.city_id,
            "X-Lavka-Web-Locale": "ru",
        }

    def _request_text(
        self,
        path: str,
        *,
        method: str,
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        include_csrf: bool = True,
    ) -> str:
        if self._before_request is not None:
            self._before_request()
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        request_headers = {
            "Accept-Language": "ru-RU,ru;q=0.9",
            "User-Agent": "Mozilla/5.0 (compatible; basket-comparison-research/0.1)",
        }
        if include_csrf:
            request_headers.update(self._headers())
        if headers:
            request_headers.update(headers)
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(url, data=data, headers=request_headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8")
        except HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            self._raise_http_error(error.code, raw, error.headers.get("Retry-After"))
        except (TimeoutError, socket.timeout) as error:
            raise LavkaError("TIMEOUT", "Lavka request timed out.") from error
        except URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise LavkaError("TIMEOUT", "Lavka request timed out.") from error
            raise LavkaError("UPSTREAM_UNAVAILABLE", f"Lavka is unavailable: {error.reason}") from error
        raise AssertionError("unreachable")

    @staticmethod
    def _raise_http_error(status: int, raw_body: str, retry_after: str | None) -> None:
        lowered = raw_body.lower()
        upstream_name = ""
        upstream_message = ""
        try:
            payload = json.loads(raw_body)
            data = payload.get("data", payload) if isinstance(payload, dict) else {}
            if isinstance(data, dict):
                upstream_name = str(data.get("name", ""))
                upstream_message = str(data.get("message", ""))
        except json.JSONDecodeError:
            pass

        combined = f"{upstream_name} {upstream_message} {lowered}".lower()
        if status == 429:
            raise LavkaError(
                "RATE_LIMITED",
                "Lavka rate limit was reached.",
                status_code=status,
                retry_after=retry_after,
            )
        if "captcha" in combined or "antirobot" in combined:
            raise LavkaError("ANTI_BOT_CHALLENGE", "Lavka requested an anti-bot check.", status_code=status)
        if status == 401 and "csrf" in combined:
            raise LavkaError("SESSION_EXPIRED", "Lavka guest session or CSRF token expired.", status_code=status)
        if "depot_not_found" in combined:
            raise LavkaError(
                "LOCATION_UNSUPPORTED",
                "Lavka has no depot for this address and storefront.",
                status_code=status,
            )
        if status in {401, 403}:
            raise LavkaError("AUTH_REQUIRED", "Lavka rejected the guest session.", status_code=status)
        if "cart_not_found" in combined:
            raise LavkaError("SESSION_EXPIRED", "Lavka cart expired.", status_code=status)
        if status >= 500:
            raise LavkaError("UPSTREAM_UNAVAILABLE", "Lavka returned a server error.", status_code=status)
        raise LavkaError(
            "UNKNOWN_ERROR",
            upstream_message or f"Lavka returned HTTP {status}.",
            status_code=status,
            details={"upstream_name": upstream_name} if upstream_name else None,
        )

    def _request_json(
        self,
        path: str,
        *,
        method: str = "POST",
        body: Mapping[str, Any] | None = None,
        referer: str | None = None,
    ) -> dict[str, Any]:
        raw = self._request_text(
            path,
            method=method,
            body=body,
            headers=self._headers(referer=referer),
        )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise LavkaError("UPSTREAM_CHANGED", "Lavka returned non-JSON data.") from error
        if not isinstance(parsed, dict):
            raise LavkaError("UPSTREAM_CHANGED", "Lavka returned an unexpected JSON shape.")
        return parsed

    def geocode(self, address: str) -> dict[str, Any]:
        return self._request_json(
            "/api/v1/providers/geo/v1/geocode",
            body={"query": address, "lang": "ru", "suppressError": True, "action": "finalize"},
        )

    def resolve_location(self, location: str | Mapping[str, Any]) -> ResolvedLocation:
        depot_type = "supermarket"
        if isinstance(location, str):
            address_text = location
            supplied: Mapping[str, Any] = {}
        elif isinstance(location, Mapping):
            supplied = location
            depot_type = str(supplied.get("depot_type", depot_type))
            address_text = str(supplied.get("address", "")).strip()
            if not address_text:
                address_text = ", ".join(
                    str(supplied.get(key, "")).strip()
                    for key in ("city", "street", "house")
                    if str(supplied.get(key, "")).strip()
                )
        else:
            raise TypeError("location must be an address string or a mapping")

        if depot_type not in ALLOWED_DEPOT_TYPES:
            allowed = ", ".join(ALLOWED_DEPOT_TYPES)
            raise ValueError(f"depot_type must be one of: {allowed}")

        has_coordinates = all(
            supplied.get(key) is not None for key in ("longitude", "latitude")
        )
        has_address_parts = all(str(supplied.get(key, "")).strip() for key in ("city", "street", "house"))
        if has_coordinates and has_address_parts:
            geocoded = {
                "city": supplied["city"],
                "street": supplied["street"],
                "house": supplied["house"],
                "lon": supplied["longitude"],
                "lat": supplied["latitude"],
                "buildingId": supplied.get("building_id"),
            }
        else:
            if not address_text:
                raise ValueError("location must contain an address or full address parts and coordinates")
            geocoded = self.geocode(address_text)

        try:
            city = str(geocoded["city"])
            street = str(geocoded["street"])
            house = str(geocoded["house"])
            longitude = float(geocoded["lon"])
            latitude = float(geocoded["lat"])
        except (KeyError, TypeError, ValueError) as error:
            raise LavkaError("LOCATION_UNSUPPORTED", "Lavka could not resolve the delivery address.") from error

        provisional = ResolvedLocation(
            city=city,
            street=street,
            house=house,
            longitude=longitude,
            latitude=latitude,
            building_id=str(geocoded.get("buildingId") or "") or None,
            depot_type=depot_type,
        )
        info = self.get_service_info(provisional)
        if not info.get("depotId"):
            raise LavkaError("LOCATION_UNSUPPORTED", "Lavka did not select a depot for this address.")
        return ResolvedLocation(
            city=city,
            street=street,
            house=house,
            longitude=longitude,
            latitude=latitude,
            building_id=provisional.building_id,
            depot_type=depot_type,
            depot_id=str(info["depotId"]),
            currency=str(info.get("currency") or "RUB"),
            currency_sign=str(info.get("currencySign") or "₽"),
            service_info=info,
        )

    def get_service_info(self, location: ResolvedLocation) -> dict[str, Any]:
        params = {
            "additionalData[city]": location.city,
            "additionalData[street]": location.street,
            "additionalData[house]": location.house,
            "position[location][0]": location.longitude,
            "position[location][1]": location.latitude,
            "fallbackCurrencySign": location.currency_sign,
            "depotType": location.depot_type,
        }
        return self._request_json(
            f"/api/v1/providers/v2/service-info?{urlencode(params)}",
            method="GET",
        )

    @staticmethod
    def _location_fields(location: ResolvedLocation) -> dict[str, Any]:
        result: dict[str, Any] = {
            "additionalData": location.additional_data,
            "position": location.position,
            "depotType": location.depot_type,
        }
        if location.building_id:
            result["geoId"] = location.building_id
        return result

    def search_products(self, query: str, location: ResolvedLocation, limit: int) -> dict[str, Any]:
        body = {
            **self._location_fields(location),
            "text": query.strip(),
            "productsLimit": limit,
            "subcategoriesLimit": min(10, limit),
            "useRetail": True,
            "source": "manual_input",
            "currencySign": location.currency_sign,
        }
        return self._request_json(
            "/api/v1/providers/search/v3/lavka",
            body=body,
            referer=f"{self.base_url}/search",
        )

    def get_category_layout(
        self,
        location: ResolvedLocation,
        layout_slug: str = "grocery",
    ) -> dict[str, Any]:
        """Return the storefront category tree used by the public web app."""

        body = {
            **self._location_fields(location),
            "currencySign": location.currency_sign,
            "layoutSlug": layout_slug,
        }
        prefix = "/supermarket" if location.depot_type == "supermarket" else ""
        return self._request_json(
            "/api/v1/providers/v1/layout",
            body=body,
            referer=f"{self.base_url}{prefix}/catalog/{quote(layout_slug)}",
        )

    def get_category_products(
        self,
        location: ResolvedLocation,
        category_id: str,
        group_id: str,
        layout_slug: str = "grocery",
    ) -> dict[str, Any]:
        """Return all product cards and subcategories for one category."""

        body = {
            **self._location_fields(location),
            "currencySign": location.currency_sign,
            "modes": ["grocery"],
            "categoryId": category_id,
            "categorySlugPath": {
                "layoutSlug": layout_slug,
                "groupId": group_id,
            },
        }
        prefix = "/supermarket" if location.depot_type == "supermarket" else ""
        return self._request_json(
            "/api/v1/providers/v2/category",
            body=body,
            referer=(
                f"{self.base_url}{prefix}/catalog/{quote(layout_slug)}/category/"
                f"{quote(category_id)}"
            ),
        )

    def get_product_detail(
        self,
        location: ResolvedLocation,
        product_id: str,
    ) -> dict[str, Any]:
        """Return the complete product-card response, including unavailable goods."""

        body = {
            **self._location_fields(location),
            "currencySign": location.currency_sign,
            "productId": product_id,
            "needCatalogPaths": True,
            "isEcomboReward": False,
            "rewardPriceTemplate": "",
            "enableUnavailable": True,
        }
        return self._request_json(
            "/api/v1/providers/v1/product",
            body=body,
            referer=f"{self.base_url}/good/{quote(product_id)}",
        )

    def get_products_by_id(
        self,
        product_ids: Sequence[str],
        location: ResolvedLocation,
    ) -> dict[str, Any]:
        body = {
            **self._location_fields(location),
            "productIds": list(product_ids),
            "currencySign": location.currency_sign,
        }
        return self._request_json("/api/v1/providers/v1/products/list/by-id", body=body)

    def retrieve_cart(
        self,
        location: ResolvedLocation,
        cart_id: str | None = None,
    ) -> dict[str, Any]:
        body = {**self._location_fields(location), "isUserOrderEdit": False}
        if cart_id:
            body["cartId"] = cart_id
        return self._request_json(
            "/api/v1/providers/cart/v1/retrieve",
            body=body,
            referer=f"{self.base_url}/cart",
        )

    def update_cart(
        self,
        location: ResolvedLocation,
        cart: Mapping[str, Any],
        items: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        body = {
            **self._location_fields(location),
            "items": list(items),
            "deliveryType": cart.get("deliveryType") or "courier",
            "cartId": cart.get("cartId"),
            "cartVersion": cart.get("cartVersion"),
            "idempotencyToken": cart.get("nextIdempotencyToken") or secrets.token_hex(8),
            "isUserOrderEdit": False,
        }
        return self._request_json(
            "/api/v1/providers/cart/v1/update",
            body=body,
            referer=f"{self.base_url}/cart",
        )

    def select_personal_logistic(
        self,
        location: ResolvedLocation,
        cart: Mapping[str, Any],
        selected: bool,
    ) -> dict[str, Any]:
        body = {
            **self._location_fields(location),
            "items": [],
            "deliveryType": cart.get("deliveryType") or "eats_dispatch",
            "cartId": cart.get("cartId"),
            "cartVersion": cart.get("cartVersion"),
            "idempotencyToken": cart.get("nextIdempotencyToken") or secrets.token_hex(8),
            "isUserOrderEdit": False,
            "personalLogisticSelected": selected,
        }
        return self._request_json(
            "/api/v1/providers/cart/v1/update",
            body=body,
            referer=f"{self.base_url}/cart",
        )


class YandexLavkaParser:
    """Adapter implementing search() and full guest-cart calculation."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client_factory: Callable[[], LavkaClient] | None = None,
    ) -> None:
        self._client_factory = client_factory or (lambda: LavkaClient(timeout=timeout))

    def search(
        self,
        query: str,
        location: str | Mapping[str, Any],
        limit: int = 20,
        *,
        depot_type: str | None = None,
    ) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        if limit <= 0:
            return []
        limit = min(limit, 100)
        client = self._client_factory()
        resolved = client.resolve_location(_with_depot_type(location, depot_type))
        response = client.search_products(query, resolved, limit)
        products = response.get("cacheProducts")
        if not isinstance(products, list):
            raise LavkaError("UPSTREAM_CHANGED", "Lavka search response has no product list.")

        by_id = {str(product.get("id")): product for product in products if product.get("id")}
        ordered_ids = [
            str(item.get("id"))
            for item in response.get("layoutItems", [])
            if isinstance(item, dict) and item.get("id") in by_id
        ]
        ordered_ids.extend(product_id for product_id in by_id if product_id not in ordered_ids)

        result: list[dict[str, Any]] = []
        for product_id in ordered_ids[:limit]:
            product = by_id[product_id]
            name = _clean_text(product.get("longTitle") or product.get("title") or "")
            amount = _clean_text(product.get("amount") or "")
            quantity_value, quantity_unit = _parse_amount(amount)
            entry: dict[str, Any] = {
                "service": SERVICE_CODE,
                "depot_type": resolved.depot_type,
                "storefront": STOREFRONT_NAMES.get(resolved.depot_type, resolved.depot_type),
                "depot_id": resolved.depot_id,
                "product_id": product_id,
                "name": name,
                "price": _money_to_minor(product.get("currentPrice", 0)),
                "currency": resolved.currency,
                "available": bool(product.get("available", False)),
            }
            if product.get("oldPrice") is not None:
                entry["old_price"] = _money_to_minor(product["oldPrice"])
            if amount:
                entry["amount"] = amount
            if quantity_value is not None:
                entry["quantity_value"] = quantity_value
            if quantity_unit:
                entry["quantity_unit"] = quantity_unit
            deep_link = str(product.get("deepLink") or "")
            if deep_link:
                if deep_link.startswith("http"):
                    entry["url"] = deep_link
                else:
                    prefix = "/supermarket" if resolved.depot_type == "supermarket" else ""
                    entry["url"] = f"{BASE_URL}{prefix}/good/{quote(deep_link.strip('/'), safe='-_/')}"
            result.append(entry)
        return result

    def search_storefronts(
        self,
        query: str,
        location: str | Mapping[str, Any],
        limit: int = 20,
        depot_types: Sequence[str] = GROCERY_DEPOT_TYPES,
    ) -> dict[str, list[dict[str, Any]]]:
        return {
            depot_type: self.search(
                query,
                location,
                limit,
                depot_type=depot_type,
            )
            for depot_type in depot_types
        }

    def calculate_cart(
        self,
        items: Iterable[Mapping[str, Any]],
        location: str | Mapping[str, Any],
        *,
        depot_type: str | None = None,
        delivery_option: str = "default",
    ) -> dict[str, Any]:
        if delivery_option not in {"default", "on_demand", "personal"}:
            raise ValueError("delivery_option must be one of: default, on_demand, personal")
        requested = _normalize_requested_items(items)
        client = self._client_factory()
        resolved = client.resolve_location(_with_depot_type(location, depot_type))
        cart, unavailable = self._prepare_cart(client, resolved, requested)
        cart = self._select_delivery_option(
            client,
            resolved,
            cart,
            delivery_option,
        )

        return _normalize_cart(cart, requested, unavailable, resolved)

    def calculate_cart_options(
        self,
        items: Iterable[Mapping[str, Any]],
        location: str | Mapping[str, Any],
        *,
        depot_type: str | None = None,
    ) -> dict[str, Any]:
        """Calculate confirmed fast and slower-delivery quotes for one cart.

        Both quotes reuse the same guest session and cart. This matters because
        the unselected delivery block in Lavka's response is only a preview;
        selecting each mode makes Lavka recalculate its real conditions.
        """

        requested = _normalize_requested_items(items)
        client = self._client_factory()
        resolved = client.resolve_location(_with_depot_type(location, depot_type))
        cart, unavailable = self._prepare_cart(client, resolved, requested)

        quotes: dict[str, dict[str, Any]] = {}
        errors: dict[str, dict[str, Any]] = {}

        fast_cart = self._select_delivery_option(
            client,
            resolved,
            cart,
            "on_demand",
        )
        quotes["on_demand"] = _normalize_cart(
            fast_cart,
            requested,
            list(unavailable),
            resolved,
        )

        try:
            personal_cart = self._select_delivery_option(
                client,
                resolved,
                fast_cart,
                "personal",
            )
        except LavkaError as error:
            errors["personal"] = error.to_dict()
        else:
            quotes["personal"] = _normalize_cart(
                personal_cart,
                requested,
                list(unavailable),
                resolved,
            )

        return {
            "service": SERVICE_CODE,
            "depot_type": resolved.depot_type,
            "storefront": STOREFRONT_NAMES.get(resolved.depot_type, resolved.depot_type),
            "depot_id": resolved.depot_id,
            "quotes": quotes,
            "errors": errors,
        }

    @staticmethod
    def _prepare_cart(
        client: Any,
        resolved: ResolvedLocation,
        requested: Mapping[str, Decimal],
    ) -> tuple[Mapping[str, Any], list[dict[str, Any]]]:
        product_response = client.get_products_by_id(list(requested), resolved) if requested else {"products": []}
        raw_products = product_response.get("products", [])
        if not isinstance(raw_products, list):
            raise LavkaError("UPSTREAM_CHANGED", "Lavka product response has an unexpected shape.")
        products = {str(product.get("id")): product for product in raw_products if product.get("id")}

        unavailable: list[dict[str, Any]] = []
        update_items: list[dict[str, Any]] = []
        for product_id, quantity in requested.items():
            product = products.get(product_id)
            if not product or not product.get("available", False):
                unavailable.append({"product_id": product_id, "reason": "PRODUCT_UNAVAILABLE"})
                continue
            options = product.get("options") if isinstance(product.get("options"), dict) else {}
            update_item: dict[str, Any] = {
                "id": product_id,
                "quantity": _decimal_string(quantity),
                "price": _decimal_string(product.get("currentPrice", 0)),
                "title": "",
                "currency": resolved.currency,
            }
            for source_key, target_key in (
                ("pricePerCount", "pricePerCount"),
                ("quantityType", "quantityType"),
            ):
                if options.get(source_key) is not None:
                    update_item[target_key] = options[source_key]
            update_items.append(update_item)

        initial_cart = client.retrieve_cart(resolved)
        cart = initial_cart
        if update_items:
            updated = client.update_cart(resolved, initial_cart, update_items)
            cart_id = str(updated.get("cartId") or "")
            if not cart_id:
                raise LavkaError("UPSTREAM_CHANGED", "Lavka did not return a cart id after update.")
            cart = client.retrieve_cart(resolved, cart_id)

        return cart, unavailable

    @staticmethod
    def _select_delivery_option(
        client: Any,
        resolved: ResolvedLocation,
        cart: Mapping[str, Any],
        delivery_option: str,
    ) -> Mapping[str, Any]:
        if delivery_option == "personal":
            personal_info = cart.get("personalLogisticDeliveryInfo")
            if not isinstance(personal_info, Mapping) or not personal_info.get("enabled"):
                raise LavkaError(
                    "DELIVERY_UNAVAILABLE",
                    "Lavka personal logistic delivery is unavailable for this cart.",
                )
            updated = client.select_personal_logistic(resolved, cart, True)
            cart_id = str(updated.get("cartId") or cart.get("cartId") or "")
            if not cart_id:
                raise LavkaError(
                    "UPSTREAM_CHANGED",
                    "Lavka did not return a cart id after changing delivery.",
                )
            cart = client.retrieve_cart(resolved, cart_id)
        elif delivery_option == "on_demand" and cart.get("personalLogisticSelected"):
            updated = client.select_personal_logistic(resolved, cart, False)
            cart_id = str(updated.get("cartId") or cart.get("cartId") or "")
            if not cart_id:
                raise LavkaError(
                    "UPSTREAM_CHANGED",
                    "Lavka did not return a cart id after changing delivery.",
                )
            cart = client.retrieve_cart(resolved, cart_id)

        return cart


def _with_depot_type(
    location: str | Mapping[str, Any],
    depot_type: str | None,
) -> str | Mapping[str, Any]:
    if depot_type is None:
        return location
    if depot_type not in ALLOWED_DEPOT_TYPES:
        allowed = ", ".join(ALLOWED_DEPOT_TYPES)
        raise ValueError(f"depot_type must be one of: {allowed}")
    if isinstance(location, str):
        return {"address": location, "depot_type": depot_type}
    if isinstance(location, Mapping):
        return {**location, "depot_type": depot_type}
    raise TypeError("location must be an address string or a mapping")


def _normalize_requested_items(items: Iterable[Mapping[str, Any]]) -> dict[str, Decimal]:
    normalized: dict[str, Decimal] = {}
    for item in items:
        product_id = str(item.get("product_id") or "").strip()
        if not product_id:
            raise ValueError("Every item must contain product_id")
        try:
            quantity = Decimal(str(item.get("quantity", 1)))
        except InvalidOperation as error:
            raise ValueError(f"Invalid quantity for {product_id}") from error
        if quantity <= 0:
            raise ValueError(f"Quantity must be positive for {product_id}")
        normalized[product_id] = normalized.get(product_id, Decimal(0)) + quantity
    return normalized


def _normalize_cart(
    cart: Mapping[str, Any],
    requested: Mapping[str, Decimal],
    unavailable: list[dict[str, Any]],
    location: ResolvedLocation,
) -> dict[str, Any]:
    warnings: list[str] = []
    cart_items = cart.get("items") if isinstance(cart.get("items"), list) else []
    returned_by_id = {
        str(item.get("id")): item
        for item in cart_items
        if isinstance(item, dict) and item.get("id") and not item.get("isPackage")
    }

    normalized_items: list[dict[str, Any]] = []
    unavailable_ids = {entry["product_id"] for entry in unavailable}
    for product_id, requested_quantity in requested.items():
        raw = returned_by_id.get(product_id)
        if not raw or raw.get("isUnavailableOnDepot"):
            if product_id not in unavailable_ids:
                unavailable.append({"product_id": product_id, "reason": "PRODUCT_UNAVAILABLE"})
                unavailable_ids.add(product_id)
            continue
        actual_quantity = Decimal(str(raw.get("quantity", 0)))
        unit_price_value = raw.get("catalogPrice", raw.get("currentPrice", raw.get("price", 0)))
        old_unit_value = raw.get("fullCatalogPrice")
        unit_price = _money_to_minor(unit_price_value)
        item_result: dict[str, Any] = {
            "product_id": product_id,
            "name": _clean_text(raw.get("subtitle") or raw.get("title") or ""),
            "quantity": _number_for_json(actual_quantity),
            "unit_price": unit_price,
            "line_total": _minor_times_quantity(unit_price, actual_quantity),
            "available": True,
        }
        if old_unit_value is not None and Decimal(str(old_unit_value)) > Decimal(str(unit_price_value)):
            item_result["old_unit_price"] = _money_to_minor(old_unit_value)
        normalized_items.append(item_result)
        if actual_quantity != requested_quantity:
            warnings.append(
                f"Quantity changed for {product_id}: requested {_decimal_string(requested_quantity)}, "
                f"received {_decimal_string(actual_quantity)}."
            )

    price = cart.get("price") if isinstance(cart.get("price"), dict) else {}
    order_conditions = (
        cart.get("orderConditions") if isinstance(cart.get("orderConditions"), dict) else {}
    )
    items_after_discount = _money_to_minor(_nested_number(price, "items", default=0))
    items_subtotal = _money_to_minor(_nested_number(price, "full", default=None))
    if not items_subtotal:
        items_subtotal = sum(
            item.get("old_unit_price", item["unit_price"]) * Decimal(str(item["quantity"]))
            for item in normalized_items
        )
        items_subtotal = int(items_subtotal)
    delivery_fee = _money_to_minor(order_conditions.get("deliveryCost", 0))
    total = _money_to_minor(_nested_number(price, "total", default=0))
    discounts = max(items_subtotal - items_after_discount, 0)
    remainder = total - items_after_discount - delivery_fee
    if remainder >= 0:
        service_fees = remainder
    else:
        service_fees = 0
        discounts += -remainder

    calculated_total = items_subtotal + delivery_fee + service_fees - discounts
    if calculated_total != total:
        warnings.append(
            f"Arithmetic mismatch: components give {calculated_total}, upstream total is {total}."
        )
    if cart.get("isSurge") or cart.get("showSurge"):
        warnings.append("High-demand delivery conditions are active.")
    if unavailable:
        warnings.append(f"{len(unavailable)} requested item(s) were unavailable.")

    status = "partial" if unavailable or any(message.startswith("Quantity changed") for message in warnings) else "complete"
    minimum_remaining = _money_to_minor(
        (cart.get("requirements") or {}).get("sumToMinOrder", 0)
        if isinstance(cart.get("requirements"), dict)
        else 0
    )
    max_eta = _optional_int(order_conditions.get("maxEta"))
    min_eta = _optional_int(order_conditions.get("minEta"))
    selected_minimum_order = _money_to_minor(order_conditions.get("minimumOrderPrice", 0))
    delivery_options = _normalize_delivery_options(
        cart,
        items_after_discount=items_after_discount,
        selected_delivery_fee=delivery_fee,
        selected_minimum_order=selected_minimum_order,
        selected_minimum_remaining=minimum_remaining,
        selected_min_eta=min_eta,
        selected_max_eta=max_eta,
    )
    selected_delivery_option = next(
        (option["code"] for option in delivery_options if option.get("selected")),
        None,
    )
    result: dict[str, Any] = {
        "service": SERVICE_CODE,
        "depot_type": location.depot_type,
        "storefront": STOREFRONT_NAMES.get(location.depot_type, location.depot_type),
        "depot_id": location.depot_id,
        "status": status,
        "items": normalized_items,
        "items_subtotal": items_subtotal,
        "delivery_fee": delivery_fee,
        "service_fees": service_fees,
        "discounts": discounts,
        "total": total,
        "currency": location.currency,
        "delivery_eta_minutes": max_eta,
        "unavailable_items": unavailable,
        "calculated_at": datetime.now(timezone(timedelta(hours=3), "MSK")).isoformat(timespec="seconds"),
        "warnings": warnings,
        "available_for_checkout": bool(cart.get("availableForCheckout", False)),
        "minimum_order_remaining": minimum_remaining,
        "selected_delivery_option": selected_delivery_option,
        "delivery_options": delivery_options,
    }
    if min_eta is not None:
        result["delivery_eta_min_minutes"] = min_eta
    if max_eta is not None:
        result["delivery_eta_max_minutes"] = max_eta
    return result


def _normalize_delivery_options(
    cart: Mapping[str, Any],
    *,
    items_after_discount: int,
    selected_delivery_fee: int,
    selected_minimum_order: int,
    selected_minimum_remaining: int,
    selected_min_eta: int | None,
    selected_max_eta: int | None,
) -> list[dict[str, Any]]:
    logistic_method = str(cart.get("logisticMethod") or "")
    personal_selected = bool(cart.get("personalLogisticSelected"))
    order_conditions = (
        cart.get("orderConditions") if isinstance(cart.get("orderConditions"), Mapping) else {}
    )
    definitions = (
        ("on_demand", "onDemandDeliveryInfo", "onDemand", "Сейчас"),
        ("personal", "personalLogisticDeliveryInfo", "personal", "Подождать"),
        ("time_slots", "timeSlotsDeliveryInfo", "timeSlots", "Интервал"),
    )
    options: list[dict[str, Any]] = []
    for code, field_name, method_name, title in definitions:
        raw = cart.get(field_name)
        if not isinstance(raw, Mapping):
            continue
        pricing = raw.get("pricingConditions")
        if not isinstance(pricing, Mapping):
            pricing = {}
        minimum_order = _money_to_minor(pricing.get("minimalCartPrice", 0))
        delivery_cost = _money_to_minor(pricing.get("deliveryCost", 0))
        minimum_remaining = max(minimum_order - items_after_discount, 0)
        l10n = raw.get("l10n")
        if not isinstance(l10n, Mapping):
            l10n = {}
        template_vars = l10n.get("templateVars")
        if not isinstance(template_vars, Mapping):
            template_vars = {}
        regular_delivery_fee = _display_money_to_minor(
            template_vars.get("fullDeliveryCostRange")
        )
        if code == "personal":
            selected = personal_selected
        elif code == "on_demand":
            selected = not personal_selected and logistic_method == method_name
        else:
            selected = logistic_method == method_name
        if selected:
            delivery_cost = selected_delivery_fee
            minimum_order = selected_minimum_order
            minimum_remaining = selected_minimum_remaining
        option: dict[str, Any] = {
            "code": code,
            "title": title,
            "selected": selected,
            "enabled": bool(raw.get("enabled", False)),
            "pricing_status": "confirmed" if selected else "preview",
            "requires_selection_recalculation": not selected,
            "eta_text": (
                order_conditions.get("eta") or raw.get("eta")
                if selected
                else raw.get("eta")
            ),
            "delivery_fee": delivery_cost,
            "minimum_order": minimum_order,
            "minimum_order_remaining": minimum_remaining,
            "available_for_checkout": (
                bool(cart.get("availableForCheckout", False))
                if selected
                else bool(raw.get("enabled", False)) and minimum_remaining == 0
            ),
            "high_demand": bool(raw.get("isSurge") or raw.get("showSurge")),
            "tariff": raw.get("tariff"),
        }
        if regular_delivery_fee is not None:
            option["regular_delivery_fee"] = regular_delivery_fee
        options.append(option)

    if options:
        return options

    eta_text = order_conditions.get("eta")
    if not eta_text and selected_min_eta is not None and selected_max_eta is not None:
        eta_text = f"{selected_min_eta}–{selected_max_eta} мин"
    return [
        {
            "code": str(cart.get("deliveryType") or "current"),
            "title": "Текущий вариант",
            "selected": True,
            "enabled": True,
            "pricing_status": "confirmed",
            "requires_selection_recalculation": False,
            "eta_text": eta_text,
            "delivery_fee": selected_delivery_fee,
            "minimum_order": (
                items_after_discount + selected_minimum_remaining
                if selected_minimum_remaining
                else 0
            ),
            "minimum_order_remaining": selected_minimum_remaining,
            "available_for_checkout": bool(cart.get("availableForCheckout", False)),
            "high_demand": bool(cart.get("isSurge") or cart.get("showSurge")),
            "tariff": None,
        }
    ]


def _nested_number(mapping: Mapping[str, Any], key: str, default: Any) -> Any:
    value = mapping.get(key)
    if isinstance(value, dict):
        return value.get("number", default)
    return default


def _money_to_minor(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        amount = Decimal(str(value))
    except InvalidOperation as error:
        raise LavkaError("UPSTREAM_CHANGED", f"Invalid money value from Lavka: {value!r}") from error
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _display_money_to_minor(value: Any) -> int | None:
    if value is None or value == "":
        return None
    match = re.fullmatch(r"\s*(-?\d[\d\s\u00a0\u202f]*(?:[.,]\d+)?)\s*(?:₽|руб\.?)?\s*", str(value))
    if not match:
        return None
    normalized = re.sub(r"[\s\u00a0\u202f]", "", match.group(1)).replace(",", ".")
    return _money_to_minor(normalized)


def _minor_times_quantity(unit_price: int, quantity: Decimal) -> int:
    return int((Decimal(unit_price) * quantity).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _decimal_string(value: Any) -> str:
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    rendered = format(decimal_value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _number_for_json(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _clean_text(value: Any) -> str:
    text = html.unescape(str(value)).replace("\u00ad", "")
    text = re.sub(r"</?notr>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_amount(amount: str) -> tuple[int | float | None, str | None]:
    match = re.match(r"^\s*(\d+(?:[.,]\d+)?)\s*(.*?)\s*$", amount)
    if not match:
        return None, None
    value = Decimal(match.group(1).replace(",", "."))
    raw_unit = match.group(2).lower()
    unit_map = {
        "кг": "kg",
        "г": "g",
        "л": "l",
        "мл": "ml",
        "шт": "pcs",
        "шт.": "pcs",
    }
    return _number_for_json(value), unit_map.get(raw_unit, raw_unit or None)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
