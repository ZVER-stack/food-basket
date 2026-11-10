"""Decode the actual public Ozon tileGridDesktop format and replay an anonymous HAR.

HAR replay never makes network requests. Its CSV is explicitly a partial capture,
not a completed run of every target category or every Moscow zone.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

import ozon_fresh_fast as core


def decode(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            pass
    return value


def widgets(data):
    return {key.removeprefix("state-"): decode(value) for key, value in data.get("widgetStates", {}).items()}


def widget(data, prefix):
    found = [v for k,v in widgets(data).items() if k.startswith(prefix)]
    if len(found) > 1:
        raise core.ParseError(f"Ambiguous widget prefix: {prefix}")
    return found[0] if found else None


class EmbeddedStates(HTMLParser):
    def __init__(self):
        super().__init__()
        self.states = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id", "").startswith("state-") and "data-state" in attrs:
            name = attrs["id"].removeprefix("state-")
            if name in self.states:
                raise core.ParseError(f"Duplicate embedded widget: {name}")
            self.states[name] = decode(attrs["data-state"])


def document_states(text):
    parser = EmbeddedStates()
    parser.feed(text)
    return {"widgetStates": parser.states}


def address_title(data):
    address = widget(data, "addressBookBarWeb-")
    if not isinstance(address, dict):
        return None
    # Individual actionMode with "Москва / Уточнить адрес" is not a selected house.
    cell = address.get("customCell", {})
    if cell.get("actionMode") != "common":
        return ""
    return " ".join(c.get("text", {}).get("text", "") for c in cell.get("cells", [])).strip()


def normalized_address(value):
    value = html.unescape(value).casefold().replace("ё", "е")
    # Published kilometer abbreviation; keep the kilometer number and full house.
    value = re.sub(r"\bкм\.?(?=\s|,|$)", "километр", value)
    # Observed Ozon abbreviation: retain the alley street type and full house.
    value = re.sub(r"\bал\.(?=\s|,|$)", "аллея", value)
    # Ozon's published "пр-д" means "проезд". Keep the street type rather
    # than dropping it; "пр." is ambiguous and must not be guessed.
    value = re.sub(r"\bпр\s*[-‐‑‒–—]\s*д\b\.?", "проезд", value)
    # Exact published abbreviation of "проспект"; do not expand ambiguous "пр.".
    value = re.sub(r"\bпр\s*[-‐‑‒–—]\s*кт\b\.?", "проспект", value)
    # Preserve the building number while equating observed Russian abbreviations.
    # Coordinates are also verified by the live address protocol.
    for pattern,label in ((r"(?<![а-яa-z])(?:строение|стр\.?|с)\s*(\d+[а-я]?)\b", "строение"),
                          (r"(?<![а-яa-z])(?:корпус|корп\.?|к\.?)\s*(\d+[а-я]?)\b", "корпус")):
        value = re.sub(pattern, lambda match: ' '+label+' '+match[1]+' ', value)
    value = re.sub(r"\b(?:город|москва|улица|ул|проспект|просп|переулок|пер|набережная|наб|шоссе|ш|бульвар|б-р|площадь|пл)\b\.?", " ", value)
    return re.sub(r"[^\w/]+", " ", value).strip()


def ordinary_and_card(price_widget):
    style = price_widget.get("priceStyle", {}).get("styleType")
    prices = defaultdict(list)
    for part in price_widget.get("price", []):
        if part.get("text") and part.get('textStyle') in ('PRICE','ORIGINAL_PRICE'):
            prices[part.get("textStyle")].append(core.number(part["text"]))
    if any(len(set(values)) != 1 for values in prices.values()):
        raise core.ParseError("Conflicting values for a price role")
    values = {key: values[0] for key,values in prices.items()}
    if not values.get("PRICE"):
        raise core.ParseError("Missing displayed PRICE")
    result = {"price": None, "price_regular": values.get("ORIGINAL_PRICE"), "price_card": None}
    discount = re.fullmatch(r"[−-]?(\d+(?:[.,]\d+)?)%", str(price_widget.get("discount") or ""))
    result["discount_percent"] = core.number(discount[1]) if discount else None
    bases = {str(part['text']).removeprefix('/').strip() for part in price_widget.get('price', [])
             if part.get('textStyle') == 'PPU' and part.get('text')}
    if bases:
        if len(bases) != 1 or full_amount(next(iter(bases))) is None:
            raise core.ParseError('Ambiguous or invalid displayed unit-price basis')
        result['unit_price'] = values['PRICE']
        result['unit_price_basis'] = next(iter(bases))
    if style in ("ACTUAL_PRICE", "SALE_PRICE"):
        result["price"] = values["PRICE"]
        result["price_regular"] = result["price_regular"] or result["price"]
    elif style == "CARD_PRICE":
        result["price_card"] = values["PRICE"]
        # No inference of an ordinary discounted price from a crossed-out list price.
    else:
        raise core.ParseError(f"Unknown price style: {style!r}")
    if result["price"] and result["price_regular"] and float(result["price_regular"]) < float(result["price"]):
        raise core.ParseError("Crossed-out price below ordinary price")
    return result, style


def full_amount(value):
    value = html.unescape(str(value)).replace("\u00a0", " ").strip()
    # Exact quantity labels only. Numbers in ratings/percentages are not quantities.
    unit = core.AMOUNT_UNIT
    num = r"\d+(?:[.,]\d+)?"
    if re.fullmatch(rf"{num}\s*[-–—]\s*{num}\s*{unit}", value, re.I):
        return value
    if re.fullmatch(rf"{num}\s*(?:{unit}\s*)?(?:[×xх]\s*{num}\s*)?{unit}", value, re.I):
        return value
    return None


def canonical_product(url, sku):
    url = core.public_url(url)
    parts = urlsplit(url)
    match = re.search(r"/product/(?:[^/?]*-)?(\d+)/?$", parts.path)
    if not match or match[1] != str(sku):
        raise core.ParseError("SKU and product URL disagree")
    # Remove all ad/session/tracking query parameters from the exported link.
    return urlunsplit(("https", "www.ozon.ru", parts.path, "", ""))


def tile_product(card):
    sku = card.get("sku")
    if sku is None or not str(sku).isdigit():
        raise core.ParseError("Missing numeric tile SKU")
    name, amount, price_data, style = None, None, None, None
    rating, reviews, labels = None, None, []
    for atom in card.get("mainState", []):
        kind = atom.get("type")
        value = atom.get(kind, {})
        if kind == "priceV2":
            if price_data is not None:
                raise core.ParseError("Multiple priceV2 atoms on one tile")
            price_data, style = ordinary_and_card(value)
        if atom.get("id") == "name" or value.get("testInfo", {}).get("automatizationId") == "tile-name":
            candidate = html.unescape(value.get("text", "")).strip()
            if name and candidate != name:
                raise core.ParseError("Conflicting tile names")
            name = candidate
        if kind == "labelListV2":
            test_id = value.get("testInfo", {}).get("automatizationId")
            if test_id == "tile-list-labels":
                for label in value.get("items", []):
                    candidate = label.get("text", {}).get("text")
                    if candidate:
                        amount = amount or full_amount(candidate)
            if test_id == "tile-list-rating":
                expected = None
                for part in value.get("items", []):
                    if part.get("type") == "icon":
                        icon = part.get("icon", {}).get("icon", {}).get("icon", "")
                        expected = "rating" if "star" in icon else "reviews" if "dialog" in icon else None
                    elif part.get("type") == "text":
                        text = part.get("text", {}).get("text", "")
                        if expected == "rating":
                            rating = core.number(text)
                        elif expected == "reviews":
                            count = re.fullmatch(r"([\d\s\u2009\u00a0]+)\s*(?:отзыв(?:ов|а)?|оцен(?:ок|ки|ка))?", text)
                            if count:
                                reviews = core.number(count[1])
                        expected = None
    if not name or price_data is None:
        raise core.ParseError("Missing name or price atom")
    if not amount:
        matches = core.AMOUNT.findall(name)
        amount = matches[-1].strip() if matches else None
    button = card.get("multiButton", {}).get("expressButton", {})
    add = button.get("addToCart", {})
    action = add.get("actionButton", {})
    test_id = action.get("common", {}).get("testInfo", {}).get("automatizationId")
    # 131 is a delivery schema, NOT a warehouse identifier. Never use it as store_id.
    delivery_schema = action.get("common", {}).get("action", {}).get("params", {}).get("selectedDeliverySchema")
    if button.get("type") != "addToCart" or test_id != "freshAddToCart":
        raise core.ParseError("Tile has no confirmed Fresh add-to-cart action")
    action_sku = action.get("common", {}).get("action", {}).get("params", {}).get("id")
    if str(action_sku) != str(sku):
        raise core.ParseError("Fresh button action does not match the tile SKU")
    eta = re.fullmatch(r"За\s+(\d+)\s+минут(?:у|ы)?", action.get("title", ""), re.I)
    image_url = None
    for image in card.get("tileImage", {}).get("items", []):
        if image.get("type") == "image":
            image_url = image.get("image", {}).get("link")
            break
    for badge_key in ("leftBottomBadgeV2", "topLeftBadge", "bottomBadge"):
        badge = card.get("tileImage", {}).get(badge_key, {})
        if isinstance(badge.get("text"), str):
            labels.append(badge["text"])
    return {"product_id": str(sku), "name": name, **price_data, "amount": amount,
            "in_stock": "true", "rating": rating, "reviews_count": reviews,
            "image_url": image_url, "url": canonical_product(card.get("action", {}).get("link", ""), sku),
            "labels": "; ".join(labels), "delivery_eta_min": eta[1] if eta else None,
            "extra": {"price_style": style, "fresh_action_verified": True, "delivery_schema": str(delivery_schema) if delivery_schema is not None else None,
                      **({'unit_price_kind':'card' if style=='CARD_PRICE' else 'ordinary', 'price_basis':'displayed_unit', 'weight_is_variable':bool(amount and re.search(r'\d\s*[-–—]', amount))} if price_data.get('unit_price_basis') else {})}}


def confirmed_empty_catalog(data, state_id, request_url=None):
    """An explicit Fresh category empty state, never a missing-grid guess."""
    if not isinstance(state_id,str):return False
    info=data.get('pageInfo') or {}
    server=urlsplit(info.get('url',''));query=parse_qs(server.query)
    category=re.search(r'-(\d+)/?$',server.path)
    if (info.get('pageType')!='category' or not server.path.startswith('/category/') or
            not category or str((info.get('analyticsInfo') or {}).get('categoryId'))!=category[1] or
            query.get('miniapp')!=['supermarket'] or not address_title(data)):
        return False
    if request_url:
        requested=urlsplit(request_url)
        if requested.path.startswith('/api/entrypoint-api.'):
            requested=urlsplit(parse_qs(requested.query).get('url',[''])[0])
        requested_query=parse_qs(requested.query)
        if requested.path!=server.path or requested_query.get('miniapp')!=['supermarket']:
            return False
        if any(requested_query.get(k)!=query.get(k) for k in
                ('page','layout_page_index','sold_out_page','non_found')):
            return False
    state=widgets(data).get(state_id)
    if not isinstance(state,dict):return False
    title=state.get('titleAtom') or {};message=state.get('messageAtom') or {}
    if not isinstance(title,dict) or not isinstance(message,dict):return False
    message_text=re.sub(r'\s+',' ',re.sub(r'<[^>]*>',' ',html.unescape(str(message.get('text',''))))).strip().casefold()
    if (title.get('text')!='Ничего не нашлось' or
            (title.get('testInfo') or {}).get('automatizationId')!='not-found-title' or
            (message.get('testInfo') or {}).get('automatizationId')!='not-found-message' or
            'товар не доставляется на указанный адрес' not in message_text):
        return False
    buttons=state.get('newButtons',[])
    if not isinstance(buttons,list):return False
    for button in buttons:
        if not isinstance(button,dict):continue
        common=button.get('common') or {}
        if not isinstance(common,dict):continue
        action=common.get('action') or {}
        if not isinstance(action,dict):continue
        target=urlsplit(action.get('link',''))
        if ((common.get('testInfo') or {}).get('automatizationId')=='redirect-to-brand-seller-fresh-button' and
                target.path=='/category/supermarket-25000/' and
                parse_qs(target.query).get('miniapp')==['supermarket']):
            return True
    return False


def primary_catalog_grid(data, require_layout=False, request_url=None):
    if 'layout' not in data:
        if require_layout:raise core.ParseError('Catalog layout is missing; result scope is unverified')
        return widget(data,'tileGridDesktop-')
    ids=[];empty_ids=[];all_grids=[]
    def visit(node):
        if isinstance(node,dict):
            if node.get('component')=='tileGridDesktop':
                all_grids.append(node.get('stateId'))
                info=decode(node.get('widgetTrackingInfo',{}))
                if isinstance(info,dict) and info.get('name')=='catalog.searchResultsV2':
                    ids.append(node.get('stateId'))
            if node.get('component')=='statusWidget':
                info=decode(node.get('widgetTrackingInfo',{}))
                if isinstance(info,dict) and info.get('name')=='catalog.searchEmptyState':
                    empty_ids.append(node.get('stateId'))
            for value in node.values():visit(value)
        elif isinstance(node,list):
            for value in node:visit(value)
    visit(data['layout'])
    if not all_grids and len(empty_ids)==1 and confirmed_empty_catalog(data,empty_ids[0],request_url):
        return {'items':[],'confirmed_empty_category':True}
    if ids and empty_ids:
        raise core.ParseError('Catalog has conflicting primary products and empty-state markers')
    if len(ids)!=1 or ids[0] not in widgets(data):
        raise core.ParseError('Primary search results are absent or ambiguous; recommendation shelf is not a catalog end marker')
    return widgets(data)[ids[0]]


def primary_page(data):
    grid = primary_catalog_grid(data)
    if grid is None:
        return None
    if not isinstance(grid.get("items"), list):
        raise core.ParseError("Primary tileGridDesktop items missing")
    marker = object()
    next_page = data.get("nextPage", marker)
    if next_page is marker:
        paginator = widget(data, "infiniteVirtualPaginator-") or widget(data, "paginator-")
        next_page = paginator.get("nextPage", marker) if paginator is not None else marker
    if next_page is marker:
        raise core.ParseError("Neither root nor paginator confirms the next/last page")
    if next_page is not None and not isinstance(next_page, str):
        raise core.ParseError("Invalid next-page link")
    return {"items": grid["items"], "page": grid.get("page"), "next": next_page,
            "terminal": next_page in (None, "")}


def empty_catalog_tail(data,request_url,confirmed_empty_parent=False,expected_coordinates=None,expected_category_url=None):
    """Observed sold-out continuation: category returns only a static separator.

    Require the published continuation stage and matching server page metadata.
    An arbitrary missing grid, recommendation shelf or first page is not an end.
    """
    requested=urlsplit(request_url);query=parse_qs(requested.query)
    server=urlsplit((data.get('pageInfo') or {}).get('url',''));server_query=parse_qs(server.query)
    try:
        if int(query.get('page',['0'])[0])<2 or int(query.get('layout_page_index',['0'])[0])<2 or int(query.get('sold_out_page',['0'])[0])<1:return False
    except ValueError:return False
    if not requested.path.startswith('/category/') or server.path!=requested.path or (data.get('pageInfo') or {}).get('pageType')!='category':return False
    if any(query.get(key)!=server_query.get(key) for key in ('page','layout_page_index','sold_out_page','miniapp')):return False
    layout=data.get('layout');states=widgets(data)
    if confirmed_empty_parent and 'layout' not in data and not states and 'nextPage' not in data:
        info=data.get('pageInfo') or {};tracking=decode(data.get('layoutTrackingInfo',{}))
        category=re.search(r'-(\d+)/?$',requested.path)
        if not expected_category_url or urlsplit(expected_category_url).path!=requested.path:return False
        if (not category or query.get('miniapp')!=['supermarket'] or query.get('non_found')!=['1'] or
                server_query.get('non_found')!=['1'] or not isinstance(tracking,dict) or
                tracking.get('pageType')!='category' or tracking.get('miniapp')!='supermarket' or
                str(tracking.get('categoryId'))!=category[1] or
                str((info.get('analyticsInfo') or {}).get('categoryId'))!=category[1] or
                str(tracking.get('layoutPageIndex'))!=query['layout_page_index'][0] or
                tracking.get('layoutContainer')!=query.get('layout_container',['default'])[0] or
                not isinstance(tracking.get('layoutId'),int) or not isinstance(tracking.get('layoutVersion'),int) or
                tracking.get('layoutId')!=info.get('layoutId') or
                tracking.get('layoutVersion')!=info.get('layoutVersion')):
            return False
        coordinates=(data.get('location') or {}).get('current',{}).get('geoCoordinate',{})
        try:
            delta=core.distance(coordinates['latitude'],coordinates['longitude'],
                expected_coordinates['latitude'],expected_coordinates['longitude'])
        except (KeyError,TypeError,ValueError):return False
        return math.isfinite(delta) and delta<=100
    if not isinstance(layout,list) or not layout or 'nextPage' in data:return False
    ids=[]
    for item in layout:
        if not isinstance(item,dict) or item.get('component')!='separator':return False
        info=decode(item.get('widgetTrackingInfo',{}))
        if not isinstance(info,dict) or info.get('name')!='cms.separator':return False
        ids.append(item.get('stateId'))
    return set(ids)==set(states) and all(isinstance(states[key],dict) and set(states[key])=={'height'} for key in ids)


def empty_separator_continuation(data, request_url, previous_page=None,
                                 expected_coordinates=None, expected_category_url=None,
                                 expected_address=None):
    """Observed empty transition to sold-out results; never a catalog end.

    The previous validated page must have published this exact URL. Preserve
    this response's explicit continuation instead of inventing a terminal page.
    """
    if not isinstance(previous_page, tuple) or len(previous_page) != 2:
        return False
    try:
        requested = urlsplit(core.public_url(request_url))
        previous = urlsplit(core.public_url(previous_page[0]))
        if core.public_url(previous_page[1]) != core.public_url(request_url):return False
        category_url = urlsplit(core.public_url(expected_category_url))
        query, prior = parse_qs(requested.query), parse_qs(previous.query)
        page, index = int(query['page'][0]), int(query['layout_page_index'][0])
        if (page < 2 or page != index or page != int(prior.get('page',['1'])[0])+1 or
                index != int(prior.get('layout_page_index',['1'])[0])+1 or
                query.get('sold_out_page') or query.get('non_found') or
                query.get('miniapp') != ['supermarket'] or prior.get('miniapp') != ['supermarket'] or
                parse_qs(category_url.query).get('miniapp') != ['supermarket'] or
                requested.path != previous.path or requested.path != category_url.path):return False
        category = re.search(r'-(\d+)/?$', requested.path)
        if not requested.path.startswith('/category/') or not category:return False
        next_url = data.get('nextPage')
        if not isinstance(next_url,str) or not next_url:return False
        following = urlsplit(core.public_url(next_url))
        expected_query = dict(query, layout_page_index=[str(index+1)], non_found=['1'], sold_out_page=['1'])
        if following.path != requested.path or parse_qs(following.query) != expected_query:return False
        info = data.get('pageInfo') or {}
        server = urlsplit(info.get('url',''))
        tracking = decode(data.get('layoutTrackingInfo',{}))
        if (server.path != requested.path or parse_qs(server.query) != query or
                info.get('pageType') != 'category' or not isinstance(tracking,dict) or
                tracking.get('pageType') != 'category' or tracking.get('miniapp') != 'supermarket' or
                str(tracking.get('categoryId')) != category[1] or
                str((info.get('analyticsInfo') or {}).get('categoryId')) != category[1] or
                str(tracking.get('layoutPageIndex')) != str(index) or
                tracking.get('layoutContainer') != query.get('layout_container',['default'])[0] or
                type(tracking.get('layoutId')) is not int or type(tracking.get('layoutVersion')) is not int or
                tracking['layoutId'] != info.get('layoutId') or
                tracking['layoutVersion'] != info.get('layoutVersion')):return False
        location = (data.get('location') or {}).get('current',{})
        point = location.get('geoCoordinate',{})
        distance = core.distance(point['latitude'],point['longitude'],
                                 expected_coordinates['latitude'],expected_coordinates['longitude'])
        address = re.sub(r'^Россия,\s*','',location.get('fullName',''),flags=re.I)
        if (not math.isfinite(distance) or distance > 100 or not expected_address or
                normalized_address(address) != normalized_address(expected_address)):return False
        layout, states = data.get('layout'), widgets(data)
        if not isinstance(layout,list) or not layout:return False
        ids = []
        for node in layout:
            if not isinstance(node,dict) or node.get('component') != 'separator':return False
            marker = decode(node.get('widgetTrackingInfo',{}))
            if not isinstance(marker,dict) or marker.get('name') != 'cms.separator':return False
            ids.append(node.get('stateId'))
        if len(set(ids)) != len(ids) or set(ids) != set(states):return False
        return all(isinstance(states[key],dict) and set(states[key]) == {'height'} and
                   type(states[key]['height']) in (int,float) and
                   math.isfinite(states[key]['height']) and states[key]['height'] >= 0 for key in ids)
    except (core.ParseError,KeyError,TypeError,ValueError,AttributeError):
        return False


def response_text(entry):
    content = entry.get("response", {}).get("content", {})
    text = content.get("text", "")
    if content.get("encoding") == "base64":
        return base64.b64decode(text).decode("utf-8-sig")
    return text


def parse_timestamp(entry):
    value = entry.get("startedDateTime", "")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise core.ParseError("HAR timestamp must include its timezone")
    return stamp.astimezone(core.MSK).strftime("%Y-%m-%d %H:%M:%S")


def write_csv(path, rows, columns=core.COLUMNS):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for raw in rows:
            row = {key: value for key,value in raw.items() if key in columns}
            if isinstance(row.get("extra"), dict):
                row["extra"] = json.dumps(row["extra"], ensure_ascii=False)
            writer.writerow(row)
    temp.replace(path)


def captured_categories(menu):
    """Select food by menu ancestry; never by substrings in a product name."""
    targets, edges, chemical_roots = {}, defaultdict(set), []
    def visit(node, path, food):
        title, url = node.get("title", ""), node.get("url", "")
        current = path + [title]
        children = node.get("childs", [])
        if url and food:
            absolute = core.public_url(url)
            parts = urlsplit(absolute)
            match = re.search(r"-(\d+)/?$", parts.path)
            if match and parts.path.startswith("/category/") and parse_qs(parts.query).get("miniapp") == ["supermarket"]:
                canonical = urlunsplit(("https", "www.ozon.ru", parts.path, "miniapp=supermarket", ""))
                targets[canonical] = {"category_id": match[1], "name": " > ".join(current), "url": canonical,
                    "scope": "food", "verified_leaf": True,
                    "leaf_definition": "terminal_in_captured_navigation", "source": "captured Fresh menu ancestry"}
                for child in children:
                    if child.get("url"):
                        edges[canonical].add(core.public_url(child["url"]))
        for child in children:
            visit(child, current, food or child.get("title", "").casefold() == "детское питание")
    for tab in menu.get("items", []):
        for column in tab.get("columns", []):
            for node in column.get("items", []):
                visit(node, [tab.get("title", "")], tab.get("title", "").casefold() == "продукты")
                if tab.get("title", "").casefold() == "дом и уют" and node.get("title", "").casefold() == "бытовая химия и хозтовары":
                    chemical_roots.append({"name": node["title"], "url": core.public_url(node["url"]),
                        "requires_refinement": True,
                        "reason": "This menu node also contains household goods; chemical subcategories must be verified before full collection.",
                        "visible_children": [{"name": c.get("title"), "url": core.public_url(c["url"])} for c in node.get("childs", []) if c.get("url")]})
    return sorted([value for url,value in targets.items() if not edges[url]], key=lambda x:x["name"]), chemical_roots


def replay(har_path, zones_path, output):
    entries = core.load_json(har_path).get("log", {}).get("entries", [])
    zones, excluded = core.load_zones(zones_path)
    addresses = defaultdict(list)
    for zone in zones:
        addresses[normalized_address(zone["address"])].append(zone)
    chosen, observations, conflicts, failures, pages = None, {}, [], [], []
    confirmations, api_without_house = {}, 0
    food_categories, chemical_roots = [], []
    price_styles = Counter()
    for entry_id, entry in enumerate(entries):
        request, response = entry.get("request", {}), entry.get("response", {})
        parts = urlsplit(request.get("url", ""))
        if parts.hostname not in ("www.ozon.ru", "ozon.ru") or response.get("status") != 200:
            continue
        if not food_categories and parts.path.endswith("/horizontalContentV1"):
            try:
                menu = json.loads(response_text(entry)).get("data", {})
                food_categories, chemical_roots = captured_categories(menu)
            except (ValueError, TypeError):
                pass
        is_document = entry.get("_resourceType") == "document"
        if not is_document and not parts.path.startswith("/api/entrypoint-api."):
            continue
        text = response_text(entry)
        if is_document:
            data = document_states(text)
            page_path = parts.path
        else:
            try:
                data = json.loads(text)
            except ValueError:
                continue
            page_path = urlsplit(parse_qs(parts.query).get("url", [""])[0]).path
        if not isinstance(data, dict):
            continue
        for key in widgets(data):
            if key.startswith("profileMenu") and not key.startswith("profileMenuAnonymous"):
                raise core.ProtectionError("Authenticated profile found; anonymous public captures only")
        title = address_title(data)
        if title is not None:
            matches = addresses.get(normalized_address(title), [])
            explicit_zone = matches[0] if len(matches) == 1 else None
            if is_document:
                chosen = explicit_zone
                if chosen:
                    confirmations[chosen["h3_index"]] = {"house": chosen["address"], "displayed_address": title, "confirmed_at_msk": parse_timestamp(entry)}
            elif explicit_zone is None:
                # Don't let a modal/background API response clear the visible document's house.
                # But don't attribute its product data to the visible house either.
                api_without_house += 1
                continue
            elif chosen is None or explicit_zone["h3_index"] != chosen["h3_index"]:
                failures.append({"entry": entry_id, "reason": "API delivery address differs from visible document"})
                continue
        if not chosen:
            continue
        try:
            page = primary_page(data)
        except core.ParseError as exc:
            failures.append({"entry": entry_id, "reason": str(exc)})
            continue
        if page is None:
            continue
        timestamp = parse_timestamp(entry)
        pages.append({"entry": entry_id, "h3_index": chosen["h3_index"], "path": page_path, "page": page["page"], "items": len(page["items"]), "terminal": page["terminal"], "captured_at_msk": timestamp})
        for card in page["items"]:
            try:
                product = tile_product(card)
            except core.ParseError as exc:
                failures.append({"entry": entry_id, "product_id": str(card.get("sku", "")), "reason": str(exc)})
                continue
            price_styles[product["extra"]["price_style"]] += 1
            product.update(service="ozon_fresh", h3_index=chosen["h3_index"], parsed_at=timestamp)
            product["extra"].update({"capture_type": "partial_anonymous_HAR", "har_entry": entry_id,
                "delivery_address": chosen["address"], "source_path": page_path,
                "primary_catalog_page": page["page"], "all_categories_complete": False,
                "product_taxonomy_verified": False, "store_id_exposed": False})
            key = (chosen["h3_index"], product["product_id"])
            previous = observations.get(key)
            if previous and any(previous.get(k) != product.get(k) for k in ("price", "price_card", "price_regular", "amount", "in_stock")):
                conflicts.append({"h3_index": chosen["h3_index"], "product_id": product["product_id"], "earlier_at": previous["parsed_at"], "later_at": timestamp, "resolution": "later_observed_offer_retained"})
            observations[key] = product
    ordinary, incomplete = [], []
    for product in observations.values():
        target = ordinary if product.get("price") is not None and product.get("amount") else incomplete
        target.append(product)
    ordinary.sort(key=lambda r: (r["h3_index"], r["product_id"]))
    incomplete.sort(key=lambda r: (r["h3_index"], r["product_id"]))
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out/"captured_partial.csv", ordinary)
    write_csv(out/"incomplete_observations.csv", incomplete)
    core.write_json(out/"food_categories.json", food_categories)
    core.write_json(out/"household_category_candidates.json", chemical_roots)
    by_zone = []
    for zid, confirmation in confirmations.items():
        zone_pages = [p for p in pages if p["h3_index"] == zid]
        products = [p for (z,_),p in observations.items() if z == zid]
        by_zone.append({"h3_index": zid, **confirmation, "products_observed": len(products),
            "ordinary_price_and_amount_known": sum(p.get("price") is not None and bool(p.get("amount")) for p in products),
            "card_price_only": sum(p.get("price") is None and p.get("price_card") is not None for p in products),
            "primary_pages_observed": sorted({p["page"] for p in zone_pages if isinstance(p["page"], int)}),
            "terminal_page_observed": any(p["terminal"] for p in zone_pages)})
    report = {"complete": False, "kind": "partial_HAR_replay_not_full_zone_collection", "network_requests": 0,
        "har_sha256": hashlib.sha256(Path(har_path).read_bytes()).hexdigest(), "har_entries": len(entries),
        "eligible_zones": len(zones), "excluded_no_house": len(excluded), "house_confirmations": by_zone,
        "products_with_known_ordinary_price_and_amount": len(ordinary), "incomplete_product_observations": len(incomplete),
        "food_category_routes_from_captured_menu": len(food_categories), "household_chemical_root_candidates": len(chemical_roots),
        "price_styles_across_observed_tiles": dict(price_styles), "primary_pages": pages,
        "ambiguous_API_address_responses_skipped": api_without_house,
        "offer_changes_within_capture": conflicts, "parse_failures": failures,
        "ordinary_prices_inferred_from_card_prices": 0, "store_id_available": False,
        "two_hour_target_measured": False,
        "limitations": ["Only the recorded selection is represented; complete food and household chemical category coverage is unverified.",
            "Pagination continues beyond the recorded pages; neither zone is a completed full catalog.",
            "No public warehouse ID was found in the inspected address/catalog widgets. Delivery schema 131 is not a warehouse ID.",
            "CARD_PRICE tiles without an ordinary price are retained only as incomplete observations.",
            "House association is confirmed by HTML address headers; it is never copied to other hexagons."]}
    core.write_json(out/"capture_report.json", report)
    print(json.dumps({k:v for k,v in report.items() if k not in ("primary_pages", "parse_failures", "offer_changes_within_capture")}, ensure_ascii=False, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--har", required=True)
    parser.add_argument("--zones", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()
    replay(args.har, args.zones, args.out_dir)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    main()
