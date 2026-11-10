"""Live anonymous Ozon Fresh protocol, verified with the user-launched Chrome.

Pilot writes observations, including explicitly missing ordinary prices. Full mode
exports a zone only when every configured category and required field is complete.
"""
import argparse
import hashlib
import json
import math
import os
import sys
import time
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qs

import ozon_fresh_fast as core
import ozon_har_catalog as tiles
import ozon_address_search as address_search
from ozon_browser_transport import BrowserTransport
from ozon_product_prices import enrich_prices
from ozon_run_lock import exclusive_browser
from ozon_runtime import keep_system_awake
from ozon_price_pool import PricePool


ROOT = Path(__file__).resolve().parent


def prepare_explicit_step_retry(gate, args):
    """Forward a freshly checked manual retry without shortening saved waits."""
    if (gate and gate.http_error_steps and getattr(args,'retry_after_pause',False) and
            getattr(args,'manual_access_confirmed',False) and gate.manual_since):
        core.write_json(gate.signal, {'confirmed_at_unix':time.time(),
            'source':'explicit_user_manual_check_launcher'})


class FreshLocation:
    def __init__(self, client, confirmation_category=None):
        self.client = client
        self.current_zone = None
        self.delivery_available = None
        self.confirmation_path = '/category/supermarket-25000/?miniapp=supermarket'
        if confirmation_category is not None:
            parts = urlsplit(core.public_url(confirmation_category['url']))
            if (confirmation_category.get('scope') not in ('food', 'household_chemicals')
                    or not parts.path.startswith('/category/')
                    or parse_qs(parts.query).get('miniapp') != ['supermarket']):
                raise core.ParseError('Address confirmation requires a verified Fresh category')
            self.confirmation_path = urlunsplit(('', '', parts.path, parts.query, ''))

    @staticmethod
    def pin(data, zone):
        mapping = tiles.widget(data, "addressEditMap-")
        if not isinstance(mapping, dict):
            raise core.ParseError("Delivery map widget missing")
        pin = mapping.get("centerPin", {})
        point = pin.get("coordinates", {})
        try:
            delta = core.distance(point["latitude"], point["longitude"], zone["address_lat"], zone["address_lon"])
        except (KeyError, TypeError, ValueError):
            raise core.ParseError("Map did not confirm the requested house coordinates") from None
        if not math.isfinite(delta) or delta > 100:
            raise core.ParseError(f"Delivery pin is {delta:.0f} m from the requested house")
        available = core.truth(pin.get("isAvailable"))
        return mapping, available

    @staticmethod
    def form_body(data, zone, full=False):
        mapping, _ = FreshLocation.pin(data, zone)
        form = {"addressTail": zone["address"]}
        if full:
            form.update(addressLabel="", apartment="", entrance="", floor="", intercom="",
                        comment="", receiverName="", receiverPhone="")
        return {"form": form, "geolocation": {"coords": {}, "isAvailable": False},
                "map": {"viewport": mapping["viewport"], "zoom": 17,
                        "previousCoordinates": mapping["centerPin"]["coordinates"]}}

    @staticmethod
    def button_link(data, expected):
        widget = tiles.widget(data, "addressEditConfirmButton-") or {}
        buttons = widget.get("buttonsWeb", [])
        found = [b for b in buttons if b.get("text") == expected]
        if len(found) != 1:
            raise core.ParseError(f"Expected one address action '{expected}'")
        if found[0].get('disabled') is True or found[0].get('theme') == 'disabled':
            raise core.ParseError('Address confirmation is disabled; Ozon has not resolved a house')
        link = found[0].get("action", {}).get("link", "")
        core.public_url(link)
        if urlsplit(link).path != "/modal/fresh":
            raise core.ParseError("Address action left the public Fresh modal")
        return link

    def switch(self, zone):
        initial = self.client.entrypoint("/modal/fresh?miniapp=supermarket")
        mapping = tiles.widget(initial, "addressEditMap-") or {}
        link = mapping.get("linkTplDragCenter", "")
        if "__lat__" not in link or "__lng__" not in link or urlsplit(link).path != "/modal/fresh":
            raise core.ParseError("Published delivery-coordinate template changed")
        link = link.replace("__lat__", str(zone["address_lat"])).replace("__lng__", str(zone["address_lon"]))
        data = self.client.entrypoint(link, {"form": {"addressTail": zone["address"]},
                                           "geolocation": {"coords": {}, "isAvailable": False}})
        _, available = self.pin(data, zone)
        self.current_zone = zone
        self.delivery_available = available
        if not available:
            return {"delivery_available": False, "store_id": "", "price_scope": "",
                    "delivery_eta_min": None, "delivery_fee": None, "min_order": None}
        if address_search.house_search_required(data, zone):
            link = address_search.resolve_house(self.client, data, zone)
            data = self.client.entrypoint(link, self.form_body(data, zone))
            _, selected_available = self.pin(data, zone)
            if selected_available is not True:
                raise core.ParseError('Search-selected house did not confirm delivery availability')
            _, field = address_search.address_field(data)
            if tiles.normalized_address(field.get('value', '')) != tiles.normalized_address(zone['address']):
                raise core.ParseError('House search selection did not confirm the requested house name')
        for expected, full in (("Привезти сюда", False), ("Сохранить адрес", True)):
            data = self.client.entrypoint(self.button_link(data, expected), self.form_body(data, zone, full))
            self.pin(data, zone)
        return self.confirm()

    def confirm(self):
        if self.current_zone is None:
            raise core.ParseError("No house was selected")
        if self.delivery_available is not True:
            raise core.ParseError("Delivery was not confirmed for the selected house")
        data = self.client.entrypoint(self.confirmation_path)
        point = data.get("location", {}).get("current", {}).get("geoCoordinate", {})
        context = core.read_context({"available": True, "lat": point.get("latitude"), "lon": point.get("longitude")},
            {"delivery_available": "/available", "lat": "/lat", "lon": "/lon"}, self.current_zone)
        title = tiles.address_title(data)
        if not title or tiles.normalized_address(title) != tiles.normalized_address(self.current_zone["address"]):
            raise core.ParseError(f"Дом не подтверждён заголовком Ozon: ожидался {self.current_zone['address']!r}; получен {title!r}")
        # City FIAS/UID and selectedDeliverySchema are intentionally not used as warehouse IDs.
        context["verified_address"] = title
        context['verified_coordinates']={'latitude':point['latitude'],'longitude':point['longitude']}
        return context


def full_config(categories):
    fields = "brand manufacturer country kcal protein fat carbs composition description shelf_life storage_conditions product_id name price price_regular price_card discount_percent amount unit_price unit_price_basis in_stock rating reviews_count image_url url labels delivery_eta_min extra".split()
    return {"card_decoder": "ozon_tile_v2", "card_mapping": {k: "/"+k for k in fields},
        "catalog_schema": {"widget_prefix": "tileGridDesktop-", "items": "/items", "next": "/nextPage",
            "primary_search_results": True,
            "pagination_widget_prefix": "infiniteVirtualPaginator-",
            "fresh_item_pointer": "/multiButton/expressButton/addToCart/actionButton/common/testInfo/automatizationId",
            "fresh_item_value": "freshAddToCart"},
        "categories": categories}


def pilot_categories(categories):
    food = [c for c in categories if c["scope"] == "food"]
    chem = [c for c in categories if c["scope"] == "household_chemicals"]
    return food[:2]+chem[:1]


def start_full_deadline_timer(args,output,stop_event):
    """The original full-pass wall budget includes cooldown and manual waits."""
    if args.mode!='full' or getattr(args,'continue_until_finished',False):return None
    import sqlite3
    from contextlib import closing
    until=getattr(args,'absolute_deadline',None)
    if until is None:
        started=time.time();path=Path(output)/'snapshot.sqlite'
        if path.exists():
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                value=db.execute("SELECT value FROM meta WHERE key='started_at'").fetchone()
            if value:started=core.datetime.strptime(value[0],'%Y-%m-%d %H:%M:%S').replace(tzinfo=core.MSK).timestamp()
        until=started+args.seconds
    reserve=min(60,max(1,args.seconds*.05))
    timer=threading.Timer(max(0,until-reserve-time.time()),stop_event.set)
    timer.daemon=True;timer.full_deadline_unix=until
    timer.collection_deadline_unix=until-reserve;timer.start()
    return timer


@exclusive_browser
@keep_system_awake
def run(args):
    categories = core.load_json(args.categories)
    if not isinstance(categories, list) or not categories:
        raise core.ParseError("A nonempty verified category list is required")
    for cat in categories:
        if cat.get("scope") not in ("food", "household_chemicals") or not cat.get("category_id"):
            raise core.ParseError("Unverified target category")
        core.public_url(cat["url"])
        if "/category/" not in cat["url"] or "miniapp=supermarket" not in cat["url"]:
            raise core.ParseError("A target category is not explicitly Fresh-scoped")
    zones, skipped = core.load_zones(args.zones)
    if args.limit:
        zones = zones[:args.limit]
    output = Path(args.out_dir)
    output.mkdir(parents=True, exist_ok=True)
    transport_state = Path(args.transport_state)
    client, snapshot, price_pool, catalog_index = None, None, None, None
    stop_event=getattr(args,'stop_event',None)
    if getattr(args,'price_workers',0) and stop_event is None:stop_event=threading.Event()
    if getattr(args,'wait_on_403',False) and stop_event is None:stop_event=threading.Event()
    if args.mode=='full' and stop_event is None:stop_event=threading.Event()
    deadline_timer=None
    gate=None
    if getattr(args,'wait_on_403',False):
        from ozon_backoff import BackoffGate
        gate=BackoffGate(output,stop_event,args.interval,
                         explicit_requested_interval=getattr(args,'explicit_requested_interval',False),
                         max_interval=getattr(args,'max_interval',None),
                         on_403_interval=getattr(args,'on_403_interval',None),
                         on_429_interval=getattr(args,'on_429_interval',None),
                         http_error_steps=getattr(args,'http_error_steps',None),
                         http_error_statuses=getattr(args,'http_error_statuses',(403,429)))
    start, observations, zone_reports = time.monotonic(), [], []
    stop_reason, exit_code = None, 0
    user_requested_stop=False
    core.write_json(output/'run_status.json',{'status':'running','pid':os.getpid(),'started_at':core.now(),
        'mode':args.mode,'complete':False,'out_dir':str(output.resolve())})
    try:
        deadline_timer=start_full_deadline_timer(args,output,stop_event)
        prepare_explicit_step_retry(gate,args)
        if args.retry_after_pause and not (gate and gate.http_error_steps):
            BrowserTransport.prepare_retry_after_pause(transport_state, interval=args.interval, cooldown=120,
                manual_access_confirmed=getattr(args,'manual_access_confirmed',False))
        if gate:gate.wait()
        client = BrowserTransport(cdp=args.cdp, interval=args.interval, seconds=math.inf if getattr(args,'continue_until_finished',False) else args.seconds,
                                  max_requests=args.max_requests, state_path=transport_state,
                                  isolated_context=getattr(args,'isolated_context',False),
                                  stop_event=stop_event,product_mode=getattr(args,'product_mode','navigation'),
                                  page_url=getattr(args,'browser_page_url',None),backoff_gate=gate,
                                  allow_duplicate_test_tabs=getattr(args,'allow_duplicate_test_tabs',False))
        absolute_deadline=getattr(args,'absolute_deadline',None)
        if absolute_deadline is not None:
            client.deadline=min(client.deadline,time.monotonic()+absolute_deadline-time.time())
        location = FreshLocation(client, confirmation_category=categories[0])
        if args.mode == "full":
            fingerprint = hashlib.sha256(Path(args.zones).read_bytes()+Path(args.categories).read_bytes()).hexdigest()
            snapshot = core.State(output/"snapshot.sqlite", fingerprint)
            started = core.datetime.strptime(snapshot.started_at(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=core.MSK)
            from ozon_catalog_index import CatalogIndex
            catalog_index = CatalogIndex(snapshot, allow_price_reuse=
                getattr(args,'price_policy','verified_each_address') in ('matching_catalog_cache','catalog_metadata_cache'),
                metadata_strategy=getattr(args,'price_policy','') == 'catalog_metadata_cache',
                reference_snapshot=getattr(args,'reference_snapshot',None),
                metadata_store=getattr(args,'metadata_store',None))
            remaining = math.inf if getattr(args,'continue_until_finished',False) else args.seconds - (core.datetime.now(core.MSK)-started).total_seconds()
            if remaining <= 0:
                raise core.BudgetError("Snapshot budget expired; use a new output directory for a new snapshot")
            client.deadline = min(client.deadline,time.monotonic()+remaining)
            used = snapshot.db.execute("SELECT value FROM metrics WHERE key='requests'").fetchone()
            client.max_requests = math.inf if getattr(args,'continue_until_finished',False) else args.max_requests - (used[0] if used else 0)
            if client.max_requests <= 0:
                raise core.BudgetError("Snapshot request budget exhausted")
            if getattr(args,'active_pointer',None):
                core.write_json(args.active_pointer,{'out_dir':str(output.resolve()),'started_at':snapshot.started_at(),
                    'started_unix':started.timestamp(),'seconds':args.seconds,'mode':'full','complete':False,
                    'continue_until_finished':getattr(args,'continue_until_finished',False)})
        if getattr(args,'catalog_workers',0) and not getattr(args,'price_workers',0):
            raise core.ParseError('Parallel catalogs require the shared reader pool')
        if getattr(args,'price_workers',0):
            if args.retry_after_pause and not (gate and gate.http_error_steps):
                for path in (output/'price_transports').glob('price_worker_*.json'):
                    if core.load_json(path).get('blocked'):
                        BrowserTransport.prepare_retry_after_pause(path,interval=args.interval,cooldown=120,
                            manual_access_confirmed=getattr(args,'manual_access_confirmed',False))
            price_pool=PricePool(args.price_workers,args.cdp,getattr(args,'browser_page_url',None),
                args.interval,client.deadline,output/'price_transports',stop_event,
                math.inf if getattr(args,'continue_until_finished',False) else args.max_requests,
                backoff_gate=gate,quarantine_incomplete=getattr(args,'quarantine_incomplete',False),
                allow_duplicate_test_tabs=getattr(args,'allow_duplicate_test_tabs',False))
        for i, zone in enumerate(zones, 1):
            if snapshot and snapshot.done(zone["h3_index"]):
                continue
            zone_start, request_start = time.monotonic(), client.count+(price_pool.report()['requests'] if price_pool else 0)
            worker_prefix=f"[worker {args.worker_id}] " if getattr(args,'isolated_context',False) else ''
            print(f"{worker_prefix}[{i}/{len(zones)}] {zone['h3_index']}: {zone['address']}", flush=True)
            context = location.switch(zone)
            if not context["delivery_available"]:
                if snapshot:
                    snapshot.mark(zone["h3_index"], "no_delivery")
                zone_reports.append({"h3_index": zone["h3_index"], "status": "no_delivery"})
                continue
            if args.mode == "pilot":
                for category in pilot_categories(categories):
                    parts = urlsplit(category["url"])
                    data = client.entrypoint(urlunsplit(("", "", parts.path, parts.query, "")))
                    header = tiles.address_title(data)
                    if header is not None and tiles.normalized_address(header) != tiles.normalized_address(zone["address"]):
                        raise core.ParseError("Catalog house differs from requested house")
                    page = tiles.primary_page(data)
                    if page is None:
                        raise core.ParseError("Category did not return the primary catalog")
                    page_rows=[]
                    for card in page["items"]:
                        try:
                            row = tiles.tile_product(card)
                        except core.ParseError as exc:
                            # A different seller is not a Fresh offer. Other data errors are reported.
                            zone_reports.append({"h3_index": zone["h3_index"], "category_id": category["category_id"], "card_error": str(exc)})
                            continue
                        page_rows.append(row)
                    if price_pool:
                        positions=[index for index,row in enumerate(page_rows) if row.get('price') is None or row.get('unit_price_basis')]
                        enriched=price_pool.enrich(zone,[page_rows[index] for index in positions])
                        for index,row in zip(positions,enriched):page_rows[index]=row
                    for row in page_rows:
                        if not price_pool and args.enrich_pilot_prices and (row.get('price') is None or row.get('unit_price_basis')):
                            row = enrich_prices(client, zone, row)
                        row.update(service="ozon_fresh", h3_index=zone["h3_index"], parsed_at=core.now(), category=category["name"])
                        row["extra"].update({"capture_type": "live_pilot_first_page_only", "all_categories_complete": False,
                            "delivery_address": zone["address"], "source_category_id": category["category_id"]})
                        observations.append(row)
                    zone_reports.append({"h3_index": zone["h3_index"], "category_id": category["category_id"],
                        "items": len(page["items"]), "has_next_page": not page["terminal"]})
                location.confirm()
                tiles.write_csv(output/"pilot_observations.csv", observations)
            else:
                def progress(category,page,products,cached):
                    if not cached:
                        from ozon_transport_probe import run_queued_transport_probe
                        run_queued_transport_probe(output,zone,categories,client,location)
                        from ozon_pagination_probe import run_queued_probe
                        run_queued_probe(output,zone,categories,client,location,snapshot)
                    core.write_json(output/'progress.json',{'h3_index':zone['h3_index'],'zone_number':i,
                        'requested_zones':len(zones),'category':category['name'],'category_id':category['category_id'],
                        'category_page':page,'unique_products_current_zone':products,'cached_page':cached,
                        'requests':client.count+(price_pool.report()['requests'] if price_pool else 0),
                        'elapsed_seconds':round(time.monotonic()-start,2),
                        'elapsed_snapshot_seconds':round((core.datetime.now(core.MSK)-started).total_seconds(),2),
                        'price_policy':getattr(args,'price_policy','verified_each_address'),
                        'catalog_workers':getattr(args,'catalog_workers',0),
                        'reader_pool':price_pool.report() if price_pool else None,
                        'price_cache':dict(catalog_index.stats),
                        'zones_processed':snapshot.db.execute("SELECT count(*) FROM zones WHERE status IN ('complete','collected_with_gaps','no_delivery')").fetchone()[0],
                        'zones_complete_without_gaps':snapshot.db.execute("SELECT count(*) FROM zones WHERE status='complete'").fetchone()[0],
                        'updated_at_msk':core.now()},best_effort=True)
                config=full_config(categories)
                config['quarantine_incomplete']=getattr(args,'quarantine_incomplete',False)
                captured_at, products = core.collect_catalog(client, config, context, snapshot, zone["h3_index"],
                    enrich_product=None if price_pool else lambda item: enrich_prices(client, zone, item),
                    enrich_products=(lambda items:price_pool.enrich(zone,items)) if price_pool else None,
                    progress=progress,catalog_index=catalog_index,
                    page_pool=price_pool if getattr(args,'catalog_workers',0) else None)
                location.confirm()
                unresolved=snapshot.quarantined_for(zone['h3_index'])
                rows = []
                for product in products:
                    extra = json.loads(product.get("extra") or "{}")
                    extra.update(catalog_captured_at=captured_at, delivery_address=zone["address"], offer_reused=False,
                                 full_zone_complete=not bool(unresolved),catalog_walk_complete=True,
                                 incomplete_products_excluded=len({x['product_id'] for x in unresolved}))
                    rows.append(dict(product, store_id="", extra=json.dumps(extra, ensure_ascii=False)))
                delta = catalog_index.difference(rows)
                visible_incomplete={x['product_id'] for x in unresolved}
                delta['absent_skus']=[sku for sku in delta['absent_skus'] if sku not in visible_incomplete]
                core.write_json(output/'assortment_changes'/f"{zone['h3_index']}.json",dict(delta,
                    h3_index=zone['h3_index'],complete_catalog=not bool(unresolved),catalog_walk_complete=True,
                    quarantined_skus=sorted(visible_incomplete),
                    note='Absent SKUs are excluded only from this address; the shared reference is retained.'))
                zone_status='collected_with_gaps' if unresolved else 'complete'
                snapshot.mark(zone["h3_index"], zone_status, rows)
                catalog_index.publish(zone['h3_index'], rows)
                if i==1 or i%10==0:snapshot.export(output/'ozon_fresh.csv')
                zone_reports.append({'h3_index':zone['h3_index'],'status':zone_status,'products':len(rows),
                    'quarantined_products':len(visible_incomplete),
                    'inferred_ordinary_prices':sum(bool(json.loads(row['extra']).get('ordinary_price_inferred')) for row in rows),
                    'new_skus':len(delta['new_skus']),'absent_skus':len(delta['absent_skus']),
                    'elapsed_seconds':round(time.monotonic()-zone_start,2)})
                # SQLite is the durable checkpoint; export once when this invocation
                # ends instead of rewriting all earlier zones after every new zone.
            print(f"{worker_prefix}  requests={client.count+(price_pool.report()['requests'] if price_pool else 0)-request_start}; seconds={time.monotonic()-zone_start:.1f}", flush=True)
    except (core.ParseError, OSError, KeyboardInterrupt) as exc:
        user_requested_stop=isinstance(exc,KeyboardInterrupt)
        if stop_event is not None:stop_event.set()
        if isinstance(exc,core.ProtectionError) and client:
            client.state.update(blocked=True,blocked_at_unix=time.time(),reason=str(exc),min_interval=max(3,client.interval))
            core.write_json(client.state_path,client.state)
            if price_pool:
                for worker in price_pool.clients.values():
                    worker.state['min_interval']=max(3,worker.interval)
                    core.write_json(worker.state_path,worker.state)
        stop_reason, exit_code = str(exc) or "Interrupted", 2
        print(stop_reason, file=sys.stderr, flush=True)
    finally:
        if deadline_timer:deadline_timer.cancel()
        budget_exhausted=bool(deadline_timer and time.time()>=deadline_timer.collection_deadline_unix)
        if budget_exhausted:stop_reason='Full pass wall-clock budget exhausted; partial progress preserved'
        pool_report=None
        if price_pool:
            price_pool.close();pool_report=price_pool.report()
        if observations:
            tiles.write_csv(output/"pilot_observations.csv", observations)
        if snapshot:
            with snapshot.db:
                snapshot.db.execute("INSERT INTO metrics VALUES('requests',?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value", (client.count if client else 0,))
            snapshot.export(output/"ozon_fresh.csv")
        processed_all = bool(snapshot and all(snapshot.done(z["h3_index"]) for z in zones))
        gap_zones=snapshot.db.execute("SELECT count(*) FROM zones WHERE status='collected_with_gaps'").fetchone()[0] if snapshot else 0
        complete = bool(processed_all and not gap_zones)
        report = {"mode": args.mode, "complete": complete, "requested_zones": len(zones), "excluded_no_house": len(skipped),
            "requests": client.count if client else 0, "elapsed_seconds": round(time.monotonic()-start, 2),
            "request_interval_seconds": client.interval if client else args.interval, "stop_reason": stop_reason,
            "pilot_product_observations": len(observations), "zone_reports": zone_reports,
            "warehouse_cache_enabled": False, "two_hour_full_pass_confirmed": False}
        if pool_report:
            report['price_pool']=pool_report
            report['coordinator_requests']=report['requests']
            report['requests']+=pool_report['requests']
        report['price_policy']=getattr(args,'price_policy','verified_each_address')
        report['catalog_workers']=getattr(args,'catalog_workers',0)
        if gate:
            report['request_interval_seconds']=gate.interval
            report['minimum_request_interval_seconds']=gate.minimum_interval
            report['maximum_request_interval_seconds']=gate.maximum_interval
            if gate.http_error_steps:
                report['http_error_steps_seconds']=list(gate.http_error_steps)
                report['http_error_level']=gate.http_error_level
            report['on_403_interval_seconds']=gate.on_403_interval
            report['fixed_403_interval_active']=gate.fixed_403_active
            report['on_429_interval_seconds']=gate.on_429_interval
            report['fixed_429_interval_active']=gate.fixed_429_active
        report['processed_all_zones']=processed_all
        report['zones_with_gaps']=gap_zones
        report['continue_until_finished']=getattr(args,'continue_until_finished',False)
        report['collection_budget_exhausted']=budget_exhausted
        report['user_requested_stop']=user_requested_stop
        report['reference_snapshot']=catalog_index.reference_snapshot if catalog_index else None
        if deadline_timer:
            report['seconds_budget']=args.seconds
            report['elapsed_wall_budget_seconds']=round(time.time()-(deadline_timer.full_deadline_unix-args.seconds),2)
            report['finished_within_budget']=time.time()<=deadline_timer.full_deadline_unix
        if snapshot:
            report['elapsed_snapshot_seconds']=round((core.datetime.now(core.MSK)-started).total_seconds(),2)
            incomplete=[dict(h3_index=h3,product_id=sku,category_id=category,error=error,product=json.loads(payload))
                for h3,sku,category,error,payload in snapshot.db.execute('SELECT h3,product_id,category_id,error,payload FROM quarantined ORDER BY h3,product_id')]
            core.write_json(output/'incomplete_products.json',incomplete)
            report['incomplete_products']=len({(row['h3_index'],row['product_id']) for row in incomplete})
        if catalog_index:
            report['catalog_index']=catalog_index.stats
            complete_rows=[row for payload, in snapshot.db.execute("SELECT payload FROM zones WHERE status IN ('complete','collected_with_gaps')")
                           for row in json.loads(payload)]
            report['inferred_ordinary_price_rows']=sum(bool(json.loads(row.get('extra') or '{}').get('ordinary_price_inferred')) for row in complete_rows)
            report['all_ordinary_prices_verified_for_address']=not report['inferred_ordinary_price_rows']
        report['requires_manual_access_check']=bool((client and client.state.get('blocked')) or
            (transport_state.exists() and core.load_json(transport_state).get('blocked')))
        if report['requires_manual_access_check']:
            report['next_action']='Open the known product in the same Chrome, restore access manually, wait the increased pause, then run06_resume_after_captcha.cmd'
            print('ОСТАНОВКА ДОСТУПА. Проверьте товар в том же Chrome, пройдите капчу вручную, если она показана. После паузы запустите 06_resume_after_captcha.cmd.',file=sys.stderr,flush=True)
        report['two_hour_full_pass_confirmed']=bool(complete and len(zones)==197 and
            snapshot and (core.datetime.now(core.MSK)-started).total_seconds()<=7200 and
            (not deadline_timer or time.time()<=deadline_timer.full_deadline_unix))
        report['two_hour_verified_price_pass_confirmed']=bool(report['two_hour_full_pass_confirmed'] and
            report.get('all_ordinary_prices_verified_for_address',False))
        core.write_json(output/"live_report.json", report)
        core.write_json(output/'run_status.json',{'status':'complete' if complete else ('finished_with_gaps' if processed_all else ('deadline_finished' if budget_exhausted else 'stopped')),'pid':os.getpid(),
            'updated_at':core.now(),'mode':args.mode,'complete':complete,'stop_reason':stop_reason,
            'requires_manual_access_check':report['requires_manual_access_check'],'out_dir':str(output.resolve()),
            'collection_budget_exhausted':budget_exhausted})
        if user_requested_stop:
            saved_status=core.load_json(output/'run_status.json')
            saved_status['user_requested_stop']=True
            core.write_json(output/'run_status.json',saved_status)
        if snapshot and getattr(args,'active_pointer',None):
            core.write_json(args.active_pointer,{'out_dir':str(output.resolve()),'started_at':snapshot.started_at(),
                'started_unix':started.timestamp(),'seconds':args.seconds,'mode':'full','complete':complete,
                'processed_all_zones':processed_all,'continue_until_finished':getattr(args,'continue_until_finished',False)})
        if client:
            client.close()
        if snapshot:
            snapshot.db.close()
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("pilot", "full"), default="pilot")
    parser.add_argument("--zones", default=str(ROOT/"hexagons.csv"))
    parser.add_argument("--categories", default=str(ROOT/"categories_live.json"))
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--transport-state", default=str(ROOT/"work/browser_transport_state.json"))
    parser.add_argument("--cdp", default="http://127.0.0.1:9223")
    parser.add_argument("--limit", type=int, default=5, help="0 means every eligible zone")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--seconds", type=int, default=7200)
    parser.add_argument("--max-requests", type=int, default=2300)
    parser.add_argument("--retry-after-pause", action="store_true", help="Explicit check after >=120 seconds; never an automatic 403 retry")
    parser.add_argument("--enrich-pilot-prices", action="store_true", help="Read product pages for pilot items missing the ordinary price")
    args = parser.parse_args()
    if args.out_dir is None:
        args.out_dir = str(ROOT/("results/live_pilot" if args.mode == "pilot" else "results/live_full"))
    if args.seconds <= 0 or args.interval < 1 or args.max_requests <= 0:
        parser.error("Positive budgets and interval >= 1 are required")
    try:
        return run(args)
    except core.ParseError as exc:
        print(str(exc),file=sys.stderr,flush=True)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
