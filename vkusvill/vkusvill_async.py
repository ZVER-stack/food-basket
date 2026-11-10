"""
Асинхронный парсер ВкусВилла по зонам (гексагонам) из hexagons.csv.

Что собираем для каждой зоны: все товары, которые доступны к доставке
по адресу зоны: название, цена, наличие (+ остаток), описание, состав, КБЖУ
и остальные колонки template.csv, где сайт их показывает.

Как это устроено (запросы найдены через DevTools -> Network на vkusvill.ru):

  1. Адрес доставки (на каждую зону своя сессия с отдельными cookies):
       GET  /goods/                                -> cookies + sessid (из #sessid_div)
       POST /ajax/address_way/get_Coords.php      set_by_coords=Y, lat, lon
            -> {"success":"Y", "addr": "...", "address_way_data": {"DELIVERY_AVAILABLE": true}}
       POST /ajax/address_way/service_apply.php   addr, lat, lon, type=delivery, type_service=1, sessid
  2. Каталог зоны: GET /goods/<категория>/?PAGEN_1=N с заголовком
       X-Requested-With: XMLHttpRequest -> только карточки (в 2 раза легче полной страницы).
     Когда адрес выбран, в каталоге только то, что можно привезти по этому адресу,
     а на карточке написан остаток: «В наличии 39 шт» / «В наличии 16.885 кг».
  3. Карточка товара: GET /goods/<slug>-<id>/ -> JSON-LD Product (описание, бренд, рейтинг,
     КБЖУ, состав, срок годности) + блоки VV23_DetailProdPageInfoDescItem (страна, изготовитель).
     От зоны не зависит, поэтому качаем ОДИН раз на товар и кэшируем в details_cache.jsonl.

Запуск:
    pip install aiohttp lxml
    python vkusvill_async.py --limit 3              # отладка на 3 зонах
    python vkusvill_async.py                        # все 201 зона
    python vkusvill_async.py --okrug ЦАО САО        # только свои округа

Лимит «1 запрос в секунду» из README сознательно не соблюдаем (договорились его
игнорировать) — скорость задаётся параллелизмом (--zones, --pages, --details).
Но на 429/403/капчу парсер всё равно ставит общую паузу и замедляется,
как просит README: защиту сервиса не обходим.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import html
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from lxml import html as lhtml

BASE = "https://vkusvill.ru"
SERVICE = "vkusvill"
SURNAME = "khramova"
MSK = ZoneInfo("Europe/Moscow")

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "template.csv")
HEXAGONS = os.path.join(HERE, "hexagons.csv")
DETAILS_CACHE = os.path.join(HERE, "details_cache.jsonl")
CACHE_VERSION = 2  # меняется, когда меняется разбор карточки: старый кэш перекачается

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}
XHR = {"X-Requested-With": "XMLHttpRequest"}

# Подборки, которые целиком повторяют товары из обычных категорий:
# их не обходим, чтобы не качать одно и то же по нескольку раз.
SKIP_SECTIONS = {
    "khity", "idei-dlya-podarkov", "vegetarianskoe-i-postnoe",
    "osoboe-pitanie", "dobraya-polka",
}
SKIP_PREFIXES = ("set-",)
# --food-only: только еда (без цветов, косметики, товаров для дома/сада/животных/детей и т. п.)
NON_FOOD = {
    "svezhie-tsvety", "kosmetika-sredstva-gigieny", "tovary-dlya-doma-i-kukhni", "sad-i-ogorod",
    "tovary-dlya-zhivotnykh", "tovary-dlya-detey", "zdorove", "detskiy-prazdnik", "catering",
}


# --------------------------------------------------------------------------- #
# мелкие помощники
# --------------------------------------------------------------------------- #
def now_msk() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S")


def clean(text) -> str:
    """HTML-сущности (&nbsp;), <br> и лишние пробелы -> обычный текст."""
    if text is None:
        return ""
    text = html.unescape(str(text))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


def num(text):
    """'16.885' / '4,8' / '145 ' -> float/int, иначе None."""
    if text is None:
        return None
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(text).replace("\xa0", "").replace(" ", ""))
    if not m:
        return None
    v = float(m.group(0).replace(",", "."))
    return int(v) if v.is_integer() else v


def strip_suppliers(text: str) -> str:
    """'25 Суток Поставщики: ...' -> '25 Суток';
    'УНАГРАНДЕ КОМПАНИ АО (УМАЛАТ ЗАО): 90 Суток' -> '90 Суток' (один поставщик)."""
    text = re.split(r"\s*Поставщики:", text, maxsplit=1)[0].strip(" ;")
    m = re.match(r"^([^:;]{2,120}):\s*(.+)$", text, flags=re.S)
    if m and not re.search(r"\d", m.group(1)) and m.group(1) == m.group(1).upper():
        text = m.group(2).strip(" ;")
    return text


def text_of(el) -> str:
    return clean(el.text_content()) if el is not None else ""


def first(tree, xpath):
    r = tree.xpath(xpath)
    return r[0] if r else None


def cls(name: str) -> str:
    """XPath-условие «у элемента есть класс name»."""
    return f"contains(concat(' ', normalize-space(@class), ' '), ' {name} ')"


# --------------------------------------------------------------------------- #
# разбор HTML
# --------------------------------------------------------------------------- #
def parse_sessid(page: str) -> str | None:
    m = re.search(r'id="sessid"[^>]*value="([^"]+)"', page) or re.search(
        r'name="sessid"[^>]*value="([^"]+)"', page
    )
    return m.group(1) if m else None


def parse_sections(page: str) -> list[str]:
    """Ссылки верхнего уровня каталога: /goods/<slug>/."""
    found = []
    for slug in re.findall(r'href="/goods/([a-z0-9-]+)/"', page):
        if slug in SKIP_SECTIONS or slug.startswith(SKIP_PREFIXES):
            continue
        if slug not in found:
            found.append(slug)
    return found


def parse_max_page(page: str) -> int:
    pages = [int(x) for x in re.findall(r"PAGEN_1=(\d+)", page)]
    return max(pages) if pages else 1


def parse_cards(page: str) -> list[dict]:
    """Карточки товаров из страницы каталога (полной или XHR-фрагмента)."""
    if "ProductCard" not in page:
        return []
    tree = lhtml.fromstring(page)
    out = []
    for c in tree.xpath(f"//div[{cls('ProductCard')}][@data-id]"):
        pid = c.get("data-id")
        link = first(c, f".//a[{cls('ProductCard__link')}]")
        href = link.get("href") if link is not None else None
        name = text_of(first(c, f".//span[{cls('js-product-v-tizer__title-text')}]")) or (
            link.get("title") if link is not None else ""
        )
        weight = text_of(first(c, f".//span[{cls('ProductCard__linkWeight')}]"))

        price = num(text_of(first(c, f".//span[{cls('js-datalayer-catalog-list-price')}]")))
        price_old = num(text_of(first(c, f".//span[{cls('js-datalayer-catalog-list-price-old')}]")))
        price_label = text_of(first(c, f".//div[{cls('ProductCard__price')}]/span[{cls('Price')}][1]"))
        per = re.search(r"/\s*(кг|шт|л|г|уп)\b", price_label)
        per_unit = per.group(1) if per else None

        rest = text_of(first(c, f".//div[{cls('ProductCard__Rest')}]"))
        btn = first(c, f".//button[{cls('js-delivery__basket--add')}]")
        stock_qty = num(rest) if "наличии" in rest else None
        if stock_qty is None and btn is not None and btn.get("data-max"):
            stock_qty = num(btn.get("data-max"))

        cat = text_of(first(c, f".//span[{cls('js-datalayer-catalog-list-category')}]"))
        img = first(c, f".//img[{cls('ProductCard__imageImg')}]")
        labels = [
            clean(x.get("title"))
            for x in c.xpath(f".//div[{cls('ProductCardLabel')}]")
            if x.get("title")
        ]
        promo = text_of(first(c, f".//div[{cls('ProductCard__notice')}]"))
        rating = num(text_of(first(c, f".//div[{cls('ProductCard__ratingText')}]")))

        if price_old is None or price_old < (price or 0):
            price_old = price
        discount = None
        if price and price_old and price_old > price:
            discount = round((price_old - price) / price_old * 100)

        amount = weight or (f"1 {per_unit}" if per_unit in ("кг", "л") else "")
        item = {
            "product_id": pid,
            "name": name,
            "price": price,
            "price_regular": price_old,
            "discount_percent": discount,
            "promo": promo or None,
            "amount": amount,
            "in_stock": "true" if rest or btn is not None else "false",
            "stock_qty": stock_qty,
            "rating": rating,
            "category": " > ".join(p.strip() for p in cat.split("//") if p.strip()) or None,
            "labels": "; ".join(labels) or None,
            "image_url": img.get("src") if img is not None else None,
            "url": BASE + href if href else None,
            # то, чему нет колонки -> уйдёт в extra
            "price_per": per_unit,
            "stock_text": rest or None,
            "_has_rest": bool(rest),
        }
        if per_unit in ("кг", "л"):
            item["unit_price"] = price
            item["unit_price_basis"] = f"1 {per_unit}"
        elif per_unit == "шт" and weight:
            g = re.fullmatch(r"([\d.,]+)\s*(г|кг|мл|л)", weight)
            if g and price:
                q = float(g.group(1).replace(",", "."))
                q = q / 1000 if g.group(2) in ("г", "мл") else q
                if q:
                    item["unit_price"] = round(price / q, 2)
                    item["unit_price_basis"] = "1 кг" if g.group(2) in ("г", "кг") else "1 л"
        out.append(item)
    return out


KBJU_RE = {
    "protein": r"белк\w*\s*([\d.,]+)",
    "fat": r"жир\w*\s*([\d.,]+)",
    "carbs": r"углевод\w*\s*([\d.,]+)",
    "kcal": r"([\d.,]+)\s*ккал",
}


def parse_kbju(text: str) -> dict:
    """КБЖУ на 100 г. Если поставщиков несколько, берём первого (остальное -> extra)."""
    if not text:
        return {}
    first_line = text.split("\n")[0]
    res = {}
    for k, rx in KBJU_RE.items():
        m = re.search(rx, first_line, flags=re.I)
        if m:
            res[k] = num(m.group(1))
    return res


def parse_details(page: str) -> dict:
    """Карточка товара -> поля, которые не зависят от зоны."""
    tree = lhtml.fromstring(page)
    product, crumbs = {}, []
    for s in tree.xpath('//script[@type="application/ld+json"]'):
        try:
            j = json.loads(s.text_content())
        except Exception:
            continue
        if j.get("@type") == "Product":
            product = j
        elif j.get("@type") == "BreadcrumbList":
            crumbs = [clean(x.get("name")) for x in j.get("itemListElement", [])]

    props = {clean(p.get("name")): clean(p.get("value")) for p in product.get("additionalProperty", [])}
    # блоки «Страна производства», «Изготовитель», «Бренд», «Аллергены» есть только в HTML
    for item in tree.xpath(f"//div[{cls('VV23_DetailProdPageInfoDescItem')}]"):
        t = first(item, f".//*[{cls('VV23_DetailProdPageInfoDescItem__Title')}]")
        d = first(item, f".//div[{cls('VV23_DetailProdPageInfoDescItem__Desc')}]")
        if t is None or d is None:
            continue
        key = text_of(t)
        val = clean(lhtml.tostring(d, encoding="unicode"))
        props.setdefault(key, val)

    def prop(prefix):
        for k, v in props.items():
            if k.lower().startswith(prefix.lower()):
                return v
        return ""

    kbju_text = prop("Пищевая и")
    # второй формат: отдельные поля «Пищевая ценность на 100 грамм. Ккал / Белки, г / Жиры, г / Углеводы, г»
    kbju_split = {}
    for k, v in props.items():
        if not k.lower().startswith("пищевая ценность"):
            continue
        low = k.lower()
        for key, word in (("kcal", "ккал"), ("protein", "белк"), ("fat", "жир"), ("carbs", "углевод")):
            if word in low and num(v) is not None:
                kbju_split[key] = num(v)
    rating = product.get("aggregateRating") or {}
    brand = product.get("brand")
    brand = brand.get("name") if isinstance(brand, dict) else brand
    images = product.get("image")
    image = images[0] if isinstance(images, list) and images else images
    manufacturer = prop("Изготовитель")
    m = re.match(r'([^:]{2,120}?):', manufacturer)
    offers = product.get("offers") or {}

    d = {
        "description": clean(product.get("description")) or prop("Описание") or None,
        "composition": prop("Состав") or None,
        "brand": clean(brand) or prop("Бренд") or None,
        "manufacturer": clean(m.group(1)) if m else (manufacturer or None),
        "country": strip_suppliers(prop("Страна производства")).capitalize() or None,
        "shelf_life": strip_suppliers(prop("Годен")) or None,
        "storage_conditions": prop("Условия хранения") or None,
        "reviews_count": rating.get("reviewCount"),
        "rating_page": num(rating.get("ratingValue")),
        "image_url_big": image,
        "amount_page": prop("Вес/объем") or None,
        "category_page": " > ".join(crumbs[2:-1]) or None,
        "allergens": prop("Аллергены") or None,
        "availability_page": str(offers.get("availability", "")).rsplit("/", 1)[-1] or None,
    }
    d.update(parse_kbju(kbju_text))
    d.update(kbju_split)
    if kbju_text and "\n" in kbju_text:
        d["kbju_all_suppliers"] = kbju_text
    return {k: v for k, v in d.items() if v not in (None, "")}


# --------------------------------------------------------------------------- #
# HTTP: повторы + общая пауза при 429/403/капче
# --------------------------------------------------------------------------- #
class Blocked(Exception):
    pass


class Gate:
    """Общая «пробка» на всех: при 429/403/капче все запросы ждут, пауза растёт."""

    def __init__(self):
        self.open_at = 0.0
        self.penalty = 5.0
        self.hits = 0

    async def wait(self):
        delay = self.open_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    def trip(self, why: str):
        # пока идёт пауза, повторные срабатывания от параллельных запросов не считаем
        if time.monotonic() < self.open_at:
            return
        self.hits += 1
        if self.penalty >= 300:
            print(f"  !!! сайт продолжает блокировать ({why}) даже после пауз — ОСТАНАВЛИВАЮСЬ. "
                  f"Подождите 30–60 мин и запустите с меньшим --zones/--pages/--details.",
                  file=sys.stderr, flush=True)
            os._exit(2)
        self.open_at = time.monotonic() + self.penalty
        print(f"  ! {why}: пауза {self.penalty:.0f} с для всех запросов", file=sys.stderr, flush=True)
        self.penalty = min(self.penalty * 2, 300)

    def relax(self):
        self.penalty = max(5.0, self.penalty * 0.9)


STATS = {"req": 0, "pages": 0, "cards": 0, "errors": 0}


class RateLimit:
    """Не больше N запросов в секунду на весь парсер (все зоны и сессии вместе)."""

    def __init__(self, rps: float):
        self.interval = 1.0 / rps if rps > 0 else 0
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        if not self.interval:
            return
        async with self.lock:
            now = time.monotonic()
            delay = self.next_at - now
            self.next_at = max(now, self.next_at) + self.interval
        if delay > 0:
            await asyncio.sleep(delay)


RATE = RateLimit(0)


class Pool:
    """Несколько независимых сессий (у каждой свои cookies).

    Сайт на Битриксе обрабатывает запросы ОДНОЙ PHP-сессии строго по очереди,
    поэтому параллелить внутри одной сессии бесполезно: 12 одновременных запросов
    в одной сессии дают ~1 запрос/с. Каждый поток берёт из пула свободную сессию.
    """

    def __init__(self, n, conn_limit=4):
        self.sessions = [
            aiohttp.ClientSession(
                headers=BASE_HEADERS, cookie_jar=aiohttp.CookieJar(),
                timeout=aiohttp.ClientTimeout(total=60),
                connector=aiohttp.TCPConnector(limit=conn_limit),
            )
            for _ in range(n)
        ]
        self.free: asyncio.Queue = asyncio.Queue()
        for x in self.sessions:
            self.free.put_nowait(x)

    async def fetch(self, gate, method, path, **kw):
        s = await self.free.get()
        try:
            return await fetch(s, gate, method, path, **kw)
        finally:
            self.free.put_nowait(s)

    async def renew(self, i):
        """Заменить i-ю сессию на совсем новую (новые cookies)."""
        old = self.sessions[i]
        self.sessions[i] = aiohttp.ClientSession(
            headers=BASE_HEADERS, cookie_jar=aiohttp.CookieJar(),
            timeout=aiohttp.ClientTimeout(total=60), connector=aiohttp.TCPConnector(limit=2),
        )
        await old.close()
        return self.sessions[i]

    async def close(self):
        await asyncio.gather(*(x.close() for x in self.sessions))


async def heartbeat(details, t0, every=15):
    """Раз в 15 с печатает, что происходит, — чтобы было видно, что парсер живой."""
    last_req, last_t = 0, time.monotonic()
    while True:
        await asyncio.sleep(every)
        now = time.monotonic()
        rps = (STATS["req"] - last_req) / (now - last_t)
        last_req, last_t = STATS["req"], now
        print(f"  … {(now - t0) / 60:.1f} мин | запросов {STATS['req']} ({rps:.1f}/с) | "
              f"страниц каталога {STATS['pages']} | карточек {STATS['cards']} | "
              f"описаний скачано {len(details.done)} из {len(details.tasks) + details.preloaded} | "
              f"ошибок {STATS['errors']} | «сессия истекла» {STATS.get('expired', 0)}", flush=True)


async def fetch(session, gate, method, path, *, data=None, headers=None, tries=5):
    url = path if path.startswith("http") else BASE + path
    last = None
    for attempt in range(tries):
        await gate.wait()
        await RATE.wait()
        STATS["req"] += 1
        try:
            async with session.request(method, url, data=data, headers=headers) as r:
                body = await r.text(errors="replace")
                if r.status in (429, 403) or "captcha" in str(r.url).lower() or (
                    r.status == 200 and "smartcaptcha" in body[:5000].lower()
                ):
                    gate.trip(f"{r.status} на {path}")
                    last = Blocked(f"{r.status} {path}")
                    continue
                if r.status >= 500:
                    last = RuntimeError(f"{r.status} {path}")
                    await asyncio.sleep(1 + attempt * 2)
                    continue
                if r.status == 404:
                    return ""
                r.raise_for_status()
                gate.relax()
                return body
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
            last = e
            STATS["errors"] += 1
            # сброс соединения сайтом = защита от нагрузки, это такая же блокировка, как 429
            if "reset" in repr(e).lower() or isinstance(e, (ConnectionResetError, aiohttp.ServerDisconnectedError)):
                gate.trip("сайт сбрасывает соединения")
                continue
            if STATS["errors"] <= 5 or STATS["errors"] % 50 == 0:
                print(f"  ! сеть: {e!r} на {path}", file=sys.stderr, flush=True)
            await asyncio.sleep(1 + attempt * 2 + random.random())
    raise last or RuntimeError(path)


# --------------------------------------------------------------------------- #
# кэш карточек товаров (общий для всех зон)
# --------------------------------------------------------------------------- #
class Details:
    def __init__(self, pool, gate):
        self.pool, self.gate = pool, gate
        self.done: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        if os.path.exists(DETAILS_CACHE):
            with open(DETAILS_CACHE, encoding="utf-8") as f:
                for line in f:
                    try:
                        j = json.loads(line)
                        if j.get("v") == CACHE_VERSION:
                            self.done[j["product_id"]] = j["d"]
                    except Exception:
                        pass
        self.preloaded = len(self.done)
        self.cache = open(DETAILS_CACHE, "a", encoding="utf-8")

    def get(self, pid: str, url: str | None) -> asyncio.Future:
        if pid in self.done:
            f = asyncio.get_running_loop().create_future()
            f.set_result(self.done[pid])
            return f
        if pid not in self.tasks:
            self.tasks[pid] = asyncio.create_task(self._load(pid, url))
        return self.tasks[pid]

    async def _load(self, pid, url):
        try:
            page = await self.pool.fetch(self.gate, "GET", url or f"/goods/{pid}/")
            d = parse_details(page) if page else {}
        except Exception as e:
            print(f"  ! карточка {pid}: {e!r}", file=sys.stderr)
            return {}  # не кэшируем: при перезапуске попробуем снова
        self.done[pid] = d
        self.cache.write(json.dumps({"product_id": pid, "v": CACHE_VERSION, "d": d}, ensure_ascii=False) + "\n")
        self.cache.flush()
        return d


# --------------------------------------------------------------------------- #
# одна зона
# --------------------------------------------------------------------------- #
async def set_address(session, gate, zone) -> tuple[bool, dict]:
    home = await fetch(session, gate, "GET", "/goods/")
    sessid = parse_sessid(home)
    coords = json.loads(await fetch(session, gate, "POST", "/ajax/address_way/get_Coords.php", headers=XHR, data={
        "set_by_coords": "Y", "addr": "", "lat": zone["address_lat"], "lon": zone["address_lon"],
        "isAddrFull": "Y", "curTypeService": 1, "checkDeliveryAvailable": "Y",
    }))
    aw = coords.get("address_way_data") or {}
    info = {"vv_address": coords.get("addr"), "sections": parse_sections(home), "no_delivery": False,
            "debug": ""}
    if coords.get("success") != "Y":
        info["debug"] = "get_Coords: " + json.dumps(coords, ensure_ascii=False)[:300]
        return False, info
    if not aw.get("DELIVERY_AVAILABLE"):
        info["no_delivery"] = True
        info["debug"] = "get_Coords без доставки: " + json.dumps(coords, ensure_ascii=False)[:400]
        return False, info
    applied = json.loads(await fetch(session, gate, "POST", "/ajax/address_way/service_apply.php", headers=XHR, data={
        "addr": coords.get("addr") or zone["address"], "addr_id": "",
        "lat": coords.get("lat"), "lon": coords.get("lon"), "dwh": "", "shop_no": "",
        "type": "delivery", "type_service": aw.get("TYPE_SERVICE", 1),
        "route_comment": "", "new_service": "", "new_slot": "", "sessid": sessid or "",
    }))
    if applied.get("success") != "Y":
        info["debug"] = f"service_apply (sessid={'есть' if sessid else 'НЕТ'}): " + \
            json.dumps(applied, ensure_ascii=False)[:300]
    return applied.get("success") == "Y", info


async def set_address_retry(session, gate, zone, tries=3):
    """«Не доставляем» тоже перепроверяем: сайт иногда отвечает так на пачку одновременных запросов."""
    info = {}
    for i in range(tries):
        ok, info = await set_address(session, gate, zone)
        if ok:
            return ok, info
        await asyncio.sleep(1.5 + i * 2 + random.random())
    return False, info


async def crawl_section(pool, gate, slug, details) -> list[dict]:
    def got(cards):
        STATS["pages"] += 1
        STATS["cards"] += len(cards)
        for c in cards:  # описание/КБЖУ начинаем качать сразу, не дожидаясь конца зоны
            details.get(c["product_id"], c.get("url"))
        return cards

    page1 = await pool.fetch(gate, "GET", f"/goods/{slug}/", headers=XHR)
    last = parse_max_page(page1)

    async def one(n):
        page = await pool.fetch(gate, "GET", f"/goods/{slug}/?PAGEN_1={n}", headers=XHR)
        return got(parse_cards(page))

    cards = got(parse_cards(page1))
    rest = await asyncio.gather(*(one(n) for n in range(2, last + 1)))
    for r in rest:
        cards.extend(r)
    for i, c in enumerate(cards, 1):
        c["section"] = slug
        c["position"] = i
    return cards


# --------------------------------------------------------------------------- #
# склады: одна точка ВкусВилла обслуживает несколько соседних зон
# --------------------------------------------------------------------------- #
# Две «отпечатковые» страницы: популярные разделы, где у товаров видны остатки.
FP_SECTIONS = ("molochnye-produkty-yaytso", "ovoshchi-frukty-yagody-zelen")
FP_MATCH = 0.8  # доля совпавших пар (товар, остаток), чтобы считать склад тем же


class Stores:
    """Склад узнаём по остаткам: у одной точки в одну минуту остатки одинаковые
    («яйца 39 шт, молоко 25 шт…»), у разных точек они почти никогда не совпадают.
    Каталог качаем один раз на склад, остальным зонам этого склада копируем."""

    def __init__(self):
        self.items: list[tuple[set, asyncio.Future, str]] = []  # (отпечаток, товары, id склада)

    def find_or_create(self, fp: set):
        best, best_score = None, 0.0
        for i, (sig, fut, sid) in enumerate(self.items):
            score = len(sig & fp) / max(1, len(sig | fp))
            if score > best_score:
                best, best_score = i, score
        if best is not None and best_score >= FP_MATCH:
            sig, fut, sid = self.items[best]
            self.items[best] = (fp, fut, sid)  # остатки меняются — держим отпечаток свежим
            return fut, sid, False, best_score
        fut = asyncio.get_running_loop().create_future()
        sid = f"fp{len(self.items) + 1:03d}"
        self.items.append((fp, fut, sid))
        return fut, sid, True, best_score


async def fingerprint(pool, gate) -> set:
    pages = await asyncio.gather(*(pool.fetch(gate, "GET", f"/goods/{sl}/", headers=XHR) for sl in FP_SECTIONS))
    fp = set()
    for page in pages:
        for c in parse_cards(page):
            fp.add((c["product_id"], c["stock_qty"]))
    return fp


class AddrStopped(Exception):
    """Сайт подряд отказывает в смене адреса — новые зоны не начинаем."""


class AddrGuard:
    """Смена адреса — самое «дорогое» для сайта действие: после серии смен он начинает
    отвечать «Сессия истекла» всем подряд. Поэтому:
      * между сменами адреса (по всему парсеру) — не меньше gap секунд;
      * после отказа — общая пауза на смену адреса: 60 с, дальше вдвое дольше (до 10 мин);
      * после max_streak отказов подряд — парсер останавливается сам."""

    def __init__(self, gap=8.0, max_streak=6):
        self.gap, self.max_streak = gap, max_streak
        self.lock = asyncio.Lock()
        self.next_at = 0.0
        self.pause = 60.0
        self.streak = 0
        self.stopped = False

    async def turn(self):
        if self.stopped:
            raise AddrStopped()
        async with self.lock:
            if self.stopped:
                raise AddrStopped()
            # перепроверяем после сна: пока ждали, мог прийти отказ и сдвинуть паузу
            while (delay := self.next_at - time.monotonic()) > 0:
                await asyncio.sleep(min(delay, 5))
            self.next_at = time.monotonic() + self.gap

    def ok(self):
        self.streak = 0
        self.pause = 60.0

    def refused(self, zone_id):
        self.streak += 1
        STATS["expired"] = STATS.get("expired", 0) + 1
        if self.streak >= self.max_streak:
            if not self.stopped:
                print(f"  !!! сайт {self.streak} раз подряд отказал в смене адреса («Сессия истекла») — "
                      f"новые зоны не начинаю, дописываю уже начатые и останавливаюсь.",
                      file=sys.stderr, flush=True)
            self.stopped = True
            raise AddrStopped()
        self.next_at = max(self.next_at, time.monotonic() + self.pause)
        print(f"    {zone_id}: «Сессия истекла» ({self.streak}-й отказ подряд) — "
              f"смену адреса откладываю на {self.pause:.0f} с", flush=True)
        self.pause = min(self.pause * 2, 600)


ADDR = AddrGuard()


async def setup_sessions(pool, idx, gate, zone, tries=3):
    """Ставит адрес в сессиях pool.sessions[idx] по очереди, через общий AddrGuard."""
    res = []
    for i in idx:
        ok, info, t = False, {}, 0
        for t in range(tries):
            await ADDR.turn()
            ok, info = await set_address(pool.sessions[i], gate, zone)
            if ok:
                ADDR.ok()
                break
            if "истекла" in info.get("debug", ""):
                ADDR.refused(zone["h3_index"])
                await pool.renew(i)
            elif info.get("no_delivery"):
                await asyncio.sleep(3)
        if ok and t > 0:
            print(f"    {zone['h3_index']}: адрес встал с попытки {t + 1}", flush=True)
        res.append((ok, info))
        if not ok and info.get("no_delivery") and i == idx[0]:
            break
    return res


SEMS: dict = {}  # "light" — адрес+отпечаток, "crawl" — полный обход нового склада


async def parse_zone(zone, args, gate, details, columns, stores) -> list[dict]:
    light = SEMS["light"]
    await light.acquire()
    light_held = True
    pool = Pool(args.pages, conn_limit=2)  # сессии создаём только когда зона реально начинается
    try:
        # 1) адрес в первой сессии
        res = await setup_sessions(pool, [0], gate, zone)
        ok, info = res[0]
        if not ok:
            if info.get("no_delivery"):
                print(f"  {zone['h3_index']}: сайт говорит, что доставки нет. {info.get('debug', '')}", flush=True)
                return []
            raise RuntimeError(f"адрес не применился: {info.get('debug')}")
        pool.free = asyncio.Queue()
        pool.free.put_nowait(pool.sessions[0])

        # 2) отпечаток склада: если такой склад уже обходили — берём его каталог
        fp = await fingerprint(pool, gate) if not args.no_stores else set()
        if fp:
            fut, sid, is_new, score = stores.find_or_create(fp)
        else:
            fut, sid, is_new, score = asyncio.get_running_loop().create_future(), "", True, 0
        if fp and not is_new:
            print(f"  {zone['h3_index']}: тот же склад {sid} (совпадение {score:.0%}) — копирую каталог", flush=True)
            light.release(); light_held = False
            await pool.close()
            products = [dict(p) for p in await fut]
            for p in products:
                p["store_fp"] = sid
        else:
            if fp:
                print(f"  {zone['h3_index']}: новый склад {sid} (лучшее совпадение {score:.0%}), "
                      f"адрес «{info['vv_address']}»", flush=True)
            light.release(); light_held = False
            try:
                # 3) новый склад: адрес в остальных сессиях и полный обход каталога
                await SEMS["crawl"].acquire()
                try:
                    products = await crawl_store(pool, gate, zone, info, args, details, sid)
                finally:
                    SEMS["crawl"].release()
                if not fut.done():
                    fut.set_result(products)
            except BaseException as e:
                if not fut.done():
                    fut.set_exception(e)
                    fut.exception()  # чтобы asyncio не ругался «exception was never retrieved»
                raise
    finally:
        if light_held:
            light.release()
        await pool.close()

    if products and not any(p.get("_has_rest") for p in products[:200]):
        print(f"  ! {zone['h3_index']}: остатков нет на карточках — похоже, адрес не применился", file=sys.stderr)

    # описания/КБЖУ: общий кэш, каждая карточка качается один раз на весь проход
    ds = await asyncio.gather(*(details.get(p["product_id"], p.get("url")) for p in products))
    out = []
    for p, d in zip(products, ds):
        p = {k: v for k, v in p.items() if not k.startswith("_")}
        for k, v in d.items():
            if k in columns and p.get(k) in (None, ""):
                p[k] = v
            elif k not in columns:
                p[k] = v
        p["vv_address"] = info["vv_address"]
        out.append(p)
    return out


async def crawl_store(pool, gate, zone, info, args, details, sid):
    """Полный обход каталога нового склада."""
    try:
        more = await setup_sessions(pool, list(range(1, len(pool.sessions))), gate, zone)
    except AddrStopped:
        more = []  # обойдёмся сессией, где адрес уже стоит
    for x, (ok2, _) in zip(pool.sessions[1:], more):
        if ok2:
            pool.free.put_nowait(x)
    sections = args.sections or info["sections"]
    if args.food_only:
        sections = [x for x in sections if x not in NON_FOOD]
    print(f"  {zone['h3_index']}: обхожу {len(sections)} разделов в {pool.free.qsize()} сессиях", flush=True)
    parts = await asyncio.gather(*(crawl_section(pool, gate, sl, details) for sl in sections))
    seen, products = set(), []
    for part in parts:
        for c in part:
            if c["product_id"] in seen:
                continue
            seen.add(c["product_id"])
            c["store_fp"] = sid or None
            products.append(c)
    return products


# --------------------------------------------------------------------------- #
# запись CSV
# --------------------------------------------------------------------------- #
def to_row(product, zone, now, columns):
    row = {k: v for k, v in product.items() if k in columns}
    rest = {k: v for k, v in product.items() if k not in columns and v not in (None, "")}
    if rest:
        row["extra"] = json.dumps(rest, ensure_ascii=False, default=str)
    row.update(service=SERVICE, h3_index=zone["h3_index"], parsed_at=now)
    return row


async def main():
    ap = argparse.ArgumentParser(description="Асинхронный парсер ВкусВилла по гексагонам")
    ap.add_argument("--rps", type=float, default=4, help="потолок запросов в секунду на весь парсер")
    ap.add_argument("--zones", type=int, default=4, help="сколько зон парсим одновременно")
    ap.add_argument("--addr-gap", type=float, default=35, help="минимум секунд между сменами адреса")
    ap.add_argument("--max-refusals", type=int, default=6,
                    help="сколько отказов «Сессия истекла» подряд терпим (пауза между ними растёт до 10 мин); "
                         "например 1000 = ждать сколько угодно, пробуя раз в 10 мин")
    ap.add_argument("--no-stores", action="store_true",
                    help="не узнавать склад по остаткам, обходить каталог в каждой зоне заново")
    ap.add_argument("--pages", type=int, default=1,
                    help="сессий на одну зону (каждая = отдельная смена адреса; сайт этого не любит)")
    ap.add_argument("--details", type=int, default=4, help="сессий для загрузки описаний товаров")
    ap.add_argument("--limit", type=int, default=0, help="только первые N зон (для отладки)")
    ap.add_argument("--okrug", nargs="*", help="только эти округа, например ЦАО САО")
    ap.add_argument("--sections", nargs="*", help="только эти разделы каталога (slug), для отладки")
    ap.add_argument("--food-only", action="store_true", help="только продуктовые разделы (без цветов, косметики, дома…)")
    ap.add_argument("--out", default=None, help="имя CSV (по умолчанию vkusvill_<дата>_khramova.csv)")
    args = ap.parse_args()
    global RATE, ADDR
    RATE = RateLimit(args.rps)
    ADDR = AddrGuard(gap=args.addr_gap, max_streak=args.max_refusals)

    with open(TEMPLATE, encoding="utf-8") as f:
        columns = next(csv.reader(f))
    with open(HEXAGONS, encoding="utf-8") as f:
        zones = list(csv.DictReader(f))
    if args.okrug:
        zones = [z for z in zones if z["okrug"] in args.okrug]
    if args.limit:
        zones = zones[: args.limit]

    out_path = args.out or os.path.join(HERE, f"{SERVICE}_{datetime.now(MSK):%Y-%m-%d}_{SURNAME}.csv")
    # продолжение после падения: зоны, которые уже есть в файле, пропускаем
    done_zones = set()
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        with open(out_path, encoding="utf-8") as f:
            done_zones = {r["h3_index"] for r in csv.DictReader(f)}
    todo = [z for z in zones if z["h3_index"] not in done_zones]
    print(f"Зон: {len(zones)}, уже готово: {len(zones) - len(todo)}, осталось: {len(todo)} -> {out_path}")

    gate = Gate()
    stores = Stores()
    write_lock = asyncio.Lock()
    SEMS["light"] = asyncio.Semaphore(args.zones)  # сколько зон одновременно ставят адрес/снимают отпечаток
    SEMS["crawl"] = asyncio.Semaphore(args.zones)  # сколько новых складов обходим одновременно
    t0 = time.monotonic()
    counter = {"n": 0}

    dpool = Pool(args.details, conn_limit=2)
    try:
        details = Details(dpool, gate)
        new_file = not os.path.exists(out_path) or os.path.getsize(out_path) == 0
        f = open(out_path, "a", encoding="utf-8", newline="")
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if new_file:
            writer.writeheader()

        async def run(zone):
            if True:
                zt = time.monotonic()
                try:
                    products = await parse_zone(zone, args, gate, details, columns, stores)
                except AddrStopped:
                    return  # зона не начата — допарсится при следующем запуске
                except Exception as e:
                    print(f"  ! {zone['h3_index']}: зона упала ({e!r}), перезапустите скрипт — она допарсится",
                          file=sys.stderr)
                    return
                now = now_msk()
                rows = [to_row(p, zone, now, columns) for p in products]
                if not rows:  # нет доставки в зону
                    rows = [{"service": SERVICE, "h3_index": zone["h3_index"], "parsed_at": now}]
                async with write_lock:
                    writer.writerows(rows)
                    f.flush()
                    counter["n"] += 1
                    el = time.monotonic() - t0
                    eta = el / counter["n"] * (len(todo) - counter["n"])
                    print(f"[{counter['n']}/{len(todo)}] {zone['h3_index']} {zone['district']}: "
                          f"{len(products)} товаров за {time.monotonic() - zt:.0f} с | "
                          f"карточек в кэше {len(details.done)} | прошло {el/60:.1f} мин, "
                          f"осталось ~{eta/60:.0f} мин | складов {len(stores.items)}")

        hb = asyncio.create_task(heartbeat(details, t0))
        await asyncio.gather(*(run(z) for z in todo))
        hb.cancel()
        f.close()
        details.cache.close()
    finally:
        await dpool.close()
    if ADDR.stopped:
        print(f"Остановлено: сайт перестал давать менять адрес. Записано зон: {counter['n']}. "
              f"Подождите 30–60 мин и запустите ту же команду — готовые зоны пропустятся.")
    print(f"Готово за {(time.monotonic() - t0) / 60:.1f} мин, блокировок: {gate.hits}. Файл: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
