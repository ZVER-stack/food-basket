"""User entry point: read the open test product, then optionally run a pilot/full pass."""
import argparse
import json
import math
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, Error as PlaywrightError
import ozon_fresh_fast as core
import ozon_har_catalog as tiles
from ozon_product_prices import product_prices, fresh_product_offer
import ozon_live_parser as live

ROOT = Path(__file__).resolve().parent
TEST_SKU = '2952307571'
MAX_SHARED_READERS = 32

def shared_output(results,resume=False,continue_expired=False):
    pointer=Path(results)/'active_shared_run.json'
    if pointer.exists():
        active=core.load_json(pointer)
        folder=Path(active['out_dir']).resolve()
        if not folder.is_relative_to(Path(results).resolve()):raise core.ParseError('Active run is outside the results directory')
        expired=time.time()-active['started_unix']>=active.get('seconds',7200)
        if (not expired or continue_expired) and not active.get('complete') and not active.get('processed_all_zones'):return folder
        if resume:raise core.BudgetError('Бюджет прежнего снимка истёк или он завершён. Файл05 начинает новый снимок; файл06 возобновляет только действующий.')
    elif resume:
        raise core.ParseError('Нет сохранённого полного запуска. Сначала запустите05.')
    return Path(results)/('full_shared_'+core.now().replace('-','').replace(':','').replace(' ','_'))

def preflight(cdp,allow_duplicate_test_tabs=False,preferred_sku=None):
    # Reads only the already-open anonymous document. Does not navigate,
    # send a site request, reset a stopped run, or import HAR cookies.
    if preferred_sku is not None and not str(preferred_sku).isdigit():
        raise core.ParseError('Preferred product SKU must be numeric')
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp, timeout=10000)
        pages = [page for context in browser.contexts for page in context.pages
                 if urlsplit(page.url).hostname in ('ozon.ru', 'www.ozon.ru')]
        duplicates=[]
        if len(pages)>1:
            if preferred_sku is not None:
                owners=[context for context in browser.contexts if any(page in context.pages for page in pages)]
                if len(owners)!=1:raise core.ParseError('Вкладки Ozon открыты в разных сессиях Chrome.')
            selected_sku=str(preferred_sku) if preferred_sku is not None else TEST_SKU
            selected=[page for page in pages if re.fullmatch(r'/product/(?:[^/]*-)?'+selected_sku+r'/?',urlsplit(page.url).path)]
            if len(selected)==1:pages=selected
            elif allow_duplicate_test_tabs and len(selected)>1 and selected_sku==TEST_SKU:
                owners=[context for context in browser.contexts if any(page in context.pages for page in selected)]
                if len(owners)!=1:raise core.ParseError('Одинаковые карточки открыты в разных сессиях Chrome.')
                # The running collector changes the session address through
                # API calls; older tab HTML can legitimately remain stale.
                # For a fresh explicit manual check use the newest test tab
                # in that SAME session, without closing the bound older tab.
                pages=[selected[-1]];duplicates=selected[:-1]
        if len(pages) != 1:
            raise core.ParseError('В этом окне Chrome оставьте одну вкладку Ozon с проверяемым товаром.')
        page = pages[0]
        if any(word in page.title().casefold() for word in ('antibot', 'captcha', 'доступ ограничен')):
            raise core.ProtectionError('В этом Chrome появилась защита. Сбор не запускался; повторных запросов нет.')
        if 'нет соединения' in page.title().casefold():
            raise core.ProtectionError('Ozon показывает «Похоже, нет соединения». Нужна ручная проверка доступа, это не подтверждение капчи.')
        sku_match=re.fullmatch(r'/product/(?:[^/]*-)?(\d+)/?', urlsplit(page.url).path)
        if not sku_match:
            raise core.ParseError('Откройте в окне из шага 01 карточку молока Parmalat, которую открыл файл запуска.')
        sku=sku_match[1]
        data = tiles.document_states(page.content())
        widgets = tiles.widgets(data)
        if (not any(key.startswith('profileMenuAnonymous') for key in widgets)
                or any(key.startswith('profileMenu') and not key.startswith('profileMenuAnonymous') for key in widgets)):
            raise core.ParseError('Нужна публичная витрина без входа в аккаунт. Сбор не запускался.')
        fresh_product_offer(data, sku)
        name = (tiles.widget(data,'webProductHeading-') or {}).get('title','')
        prices = product_prices(data,name)
        if not prices['price'] or not prices['price_regular'] or (sku==TEST_SKU and not prices['price_card']):
            raise core.ParseError('В проверяемой карточке не найдены все три цены. Сбор не запускался.')
        report = {'status':'ready','network_requests':0,'complete':False,'cdp':cdp,
                  'product_id':sku,'name':name,'prices':prices,
                  'product_url':'https://www.ozon.ru'+urlsplit(page.url).path,
                  'address':tiles.address_title(data),'anonymous':True,'fresh_offer_verified':True,
                  'note':'Reads the already-open page; live catalog requests have not yet been tested.'}
        report['older_same_session_test_tabs_ignored']=len(duplicates)
        return report

def main():
    invocation_started=time.time()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action',choices=('check','pilot','full','status'),default='check')
    parser.add_argument('--preferred-sku',help='Select the user-verified open product tab by exact SKU')
    parser.add_argument('--metadata-store',help='Read verified static metadata from the same-snapshot backfill SQLite')
    parser.add_argument('--cdp',default='http://127.0.0.1:9222')
    parser.add_argument('--interval',type=float,default=1.05)
    parser.add_argument('--max-interval',type=float,help='Maximum regular request interval; shared protection cooldown and Retry-After remain independent')
    parser.add_argument('--on-403-interval',type=float,help='Fixed minimum delay and nondecreasing worker interval after HTTP403; Retry-After and manual checks still apply')
    parser.add_argument('--on-429-interval',type=float,help='Fixed minimum delay and nondecreasing worker interval after HTTP429; Retry-After and manual checks still apply')
    parser.add_argument('--http-error-statuses',type=lambda v:tuple(int(x) for x in v.split(',')),default=(403,429))
    parser.add_argument('--http-error-steps',type=lambda value:tuple(float(x) for x in value.split(',')),
                        help='Increasing HTTP403/429 intervals, e.g. 2,3,4; Retry-After and manual checks remain independent')
    parser.add_argument('--price-workers',type=int,default=0,help='Price readers sharing the selected house;0 uses the original sequential path')
    parser.add_argument('--catalog-workers',type=int,default=0,help='Enable category concurrency using the same reader pool; must equal price-workers (no extra threads)')
    parser.add_argument('--price-policy',choices=('matching_catalog_cache','catalog_metadata_cache','verified_each_address'),
                        default='matching_catalog_cache',help='Price policies: matching cache, SKU metadata cache with product refresh on changed quotes, or per-address verification')
    parser.add_argument('--resume-after-manual-check',action='store_true',help='Explicit retry after the user restored public product access and the increased pause elapsed')
    parser.add_argument('--resume-current',action='store_true',help='Resume the current unexpired snapshot without resetting an access guard or creating a new run')
    parser.add_argument('--continue-until-finished',action='store_true',help='Explicitly authorized continuation of the same snapshot beyond its original two-hour target')
    parser.add_argument('--wait-on-403',action='store_true',help='Shared increasing cooldown in the same session; CAPTCHA needs an explicit manual restoration signal')
    parser.add_argument('--quarantine-incomplete',action='store_true',help='Keep products missing amount separately and continue; never label those zones complete')
    parser.add_argument('--reference-snapshot',help='Read completed houses from an older snapshot as a labelled SKU reference, without waiting for all197 houses')
    args = parser.parse_args()
    parsed = urlsplit(args.cdp)
    if parsed.hostname not in ('localhost','127.0.0.1') or parsed.port != 9222:
        parser.error('Этот запуск подготовлен для локального Chrome на порту 9222.')
    if not math.isfinite(args.interval) or args.interval < 1:
        parser.error('Между запросами должна быть минимум одна секунда.')
    if args.max_interval is not None and (not math.isfinite(args.max_interval) or args.max_interval<args.interval):
        parser.error('Максимальный интервал должен быть конечным и не меньше начального.')
    if args.on_403_interval is not None and (not math.isfinite(args.on_403_interval) or
            args.on_403_interval<max(1,args.interval) or
            (args.max_interval is not None and args.on_403_interval>args.max_interval)):
        parser.error('Интервал после403 должен быть конечным и находиться в пределах настройки интервала.')
    if args.on_429_interval is not None and (not math.isfinite(args.on_429_interval) or
            args.on_429_interval<max(1,args.interval) or
            (args.max_interval is not None and args.on_429_interval>args.max_interval)):
        parser.error('Интервал после429 должен быть конечным и находиться в пределах настройки интервала.')
    if args.http_error_steps is not None and (not args.http_error_steps or args.max_interval is None or
            (args.on_403_interval is not None and 403 in args.http_error_statuses) or
            (args.on_429_interval is not None and 429 in args.http_error_statuses) or
            any(not math.isfinite(x) or x<args.interval or x>args.max_interval for x in args.http_error_steps) or
            any(a>=b for a,b in zip(args.http_error_steps,args.http_error_steps[1:]))):
        parser.error('Ступени HTTP должны возрастать в пределах интервала; фиксированные интервалы 403/429 несовместимы со ступенями.')
    if not args.http_error_statuses or any(x not in (403,429) for x in args.http_error_statuses):
        parser.error('HTTP steps support only403/429')
    if not 0 <= args.price_workers <= MAX_SHARED_READERS:
        parser.error(f'Число сборщиков должно быть от 0 до {MAX_SHARED_READERS}. '
                     'Это предел настройки, а не подтверждённая скорость работы.')
    if args.catalog_workers and (args.action!='full' or args.catalog_workers!=args.price_workers):
        parser.error('Параллельные категории доступны в полном режиме: catalog-workers должен совпадать с price-workers.')
    out = ROOT/'results'
    try:
        if args.action=='status':
            from ozon_status import show_status
            show_status(ROOT)
            return 0
        out.mkdir(exist_ok=True)
        if args.resume_after_manual_check:
            from ozon_refresh import signal_refresh_manual
            if signal_refresh_manual(ROOT,args.cdp):return 0
        pointer=out/'active_shared_run.json'
        current=core.load_json(pointer) if pointer.exists() else None
        if args.resume_after_manual_check and current:
            current_folder=Path(current['out_dir'])
            if not current_folder.resolve().is_relative_to(out.resolve()):raise core.ParseError('Active run is outside the results directory')
            status_file=current_folder/'run_status.json'
            status=core.load_json(status_file) if status_file.exists() else {}
            if status.get('collection_budget_exhausted') and not current.get('continue_until_finished'):
                print('Бюджет полного прохода истёк. Файл06 его не продлевает; для нового прохода используется10.',flush=True)
                return 2
            if status.get('status') in ('waiting_manual_access','waiting_backoff'):
                preflight(args.cdp,allow_duplicate_test_tabs=True,**({'preferred_sku':args.preferred_sku} if args.preferred_sku else {}))
                core.write_json(current_folder/'manual_access_restored.json',{'confirmed_at_unix':time.time(),'source':'explicit_user_manual_check_launcher'})
                print('Ручная проверка подтверждена. Уже работающий сборщик продолжит после увеличенной паузы.',flush=True)
                return 0
        while True:
            try:
                report = preflight(args.cdp,allow_duplicate_test_tabs=True,**({'preferred_sku':args.preferred_sku} if args.preferred_sku else {}))
                break
            except core.ProtectionError as exc:
                if not args.wait_on_403 or not current or not args.continue_until_finished:raise
                from ozon_backoff import BackoffGate
                import threading
                gate=BackoffGate(Path(current['out_dir']),threading.Event(),args.interval,max_interval=args.max_interval,
                    on_403_interval=args.on_403_interval,on_429_interval=args.on_429_interval,
                    http_error_steps=args.http_error_steps)
                gate.require_manual(str(exc))
                print('Ожидание ручной проверки доступа в прежнем Chrome. Затем запустите06.',flush=True)
                gate.wait()
        core.write_json(out/'connection_check.json', report)
        prices=report['prices']
        print(f"Chrome подключён. Цены: текущая {prices['price']}, без скидки {prices['price_regular']}, карта {prices['price_card'] or 'не показана'}.",flush=True)
        print('Адрес в открытой карточке: '+(report['address'] or 'не выбран'),flush=True)
        if args.action == 'check':
            print('Проверка чтения страницы завершена. Запросов к Ozon не выполнялось.',flush=True)
            return 0
        mode=args.action
        run_name=('pilot' if mode=='pilot' else 'full')+('_shared' if args.price_workers else '')+'_9222'
        output=shared_output(out,args.resume_after_manual_check or args.resume_current,args.continue_until_finished) if mode=='full' and args.price_workers else out/run_name
        reference=args.reference_snapshot
        if not reference and mode=='full' and current and Path(current['out_dir']).resolve()!=output.resolve():
            reference=current['out_dir']
        namespace=argparse.Namespace(mode=mode,cdp=args.cdp,zones=str(ROOT/'hexagons.csv'),categories=str(ROOT/'categories_live.json'),
            out_dir=str(output),
            transport_state=str(ROOT/'work/browser_transport_9222.json'),
            limit=3 if mode=='pilot' else 0,interval=args.interval,seconds=900 if mode=='pilot' else 7200,
            max_requests=200 if mode=='pilot' else 6800,retry_after_pause=args.resume_after_manual_check,enrich_pilot_prices=True,
            browser_page_url=report['product_url'],price_workers=args.price_workers,
            catalog_workers=args.catalog_workers,
            max_interval=args.max_interval,
            on_403_interval=args.on_403_interval,
            on_429_interval=args.on_429_interval,
            http_error_steps=args.http_error_steps,http_error_statuses=args.http_error_statuses,
            price_policy=args.price_policy,
            reference_snapshot=reference,metadata_store=args.metadata_store,
            allow_duplicate_test_tabs=True,
            absolute_deadline=((current['started_unix'] if current and Path(current['out_dir']).resolve()==output.resolve()
                                else invocation_started)+7200) if mode=='full' and not args.continue_until_finished else None,
            continue_until_finished=args.continue_until_finished,wait_on_403=args.wait_on_403,
            quarantine_incomplete=args.quarantine_incomplete,
            manual_access_confirmed=args.resume_after_manual_check,
            active_pointer=str(out/'active_shared_run.json') if mode=='full' and args.price_workers else None,
            product_mode='api' if args.price_workers else 'navigation')
        print('Пилот: первые страницы 3 категорий для 3 зон; это не полный ассортимент.' if mode=='pilot'
              else ('Полный режим: 197 зон; продолжение до обхода всех адресов по указанию пользователя.' if args.continue_until_finished
                    else 'Полный режим: 197 зон; остановка через 2 часа. Завершение всех зон за это время пока не обеспечено.'),flush=True)
        if mode=='full':
            print(f'Общих сборщиков категорий и карточек: {args.price_workers}; интервал от {args.interval} до {args.max_interval or 60} секунд на сборщик.',flush=True)
            if args.http_error_steps:
                print('После новых эпизодов HTTP'+','.join(str(x) for x in args.http_error_statuses)+' интервал: '+', '.join(str(x) for x in args.http_error_steps)+
                      ' сек.; без автоматического снижения. Retry-After и ручная проверка сохраняются.',flush=True)
            if args.on_403_interval is not None:
                print(f'ПослеHTTP403: ожидание не меньше {args.on_403_interval}секунд; интервал больше не уменьшается. Retry-After и ручная проверка сохраняются.',flush=True)
            if args.on_429_interval is not None:
                print(f'ПослеHTTP429: ожидание не меньше {args.on_429_interval}секунд; интервал больше не уменьшается. Retry-After и ручная проверка сохраняются.',flush=True)
            print('Цены: '+('общий справочник SKU; перенос при совпадении цен каталога с явной отметкой непроверенной цены по адресу.'
                if args.price_policy=='matching_catalog_cache' else ('характеристики из справочника SKU; новая карточка при новом товаре или изменении цены.' if args.price_policy=='catalog_metadata_cache' else 'обычная цена подтверждается для каждого адреса.')),flush=True)
        if args.continue_until_finished:
            print('По указанию пользователя сбор продолжится сверх двухчасовой цели. Исходное время начала сохранено.',flush=True)
        while True:
            result=live.run(namespace)
            status=core.load_json(output/'run_status.json')
            from ozon_status import show_status
            show_status(ROOT)
            if status.get('user_requested_stop'):return result
            if status.get('collection_budget_exhausted'):return result
            if not args.wait_on_403 or not status.get('requires_manual_access_check'):return result
            from ozon_backoff import BackoffGate
            import threading
            gate=BackoffGate(output,threading.Event(),args.interval,max_interval=args.max_interval,
                on_403_interval=args.on_403_interval,on_429_interval=args.on_429_interval,
                http_error_steps=args.http_error_steps)
            gate.require_manual(status.get('stop_reason') or 'Manual access required')
            print('Ожидание ручной проверки доступа в прежнем Chrome. Затем запустите06.',flush=True)
            gate.wait()
    except KeyboardInterrupt:
        print('Остановлено пользователем. Сохранённый сбор можно продолжить файлом08.',flush=True)
        return 130
    except (core.ParseError,PlaywrightError) as exc:
        message=str(exc) if isinstance(exc,core.ParseError) else 'Нет подключения к Chrome на порту 9222. Сначала выполните шаг 01.'
        core.write_json(out/'connection_check.json',{'status':'stopped','complete':False,'reason':message})
        print('ОСТАНОВКА: '+message,file=sys.stderr,flush=True)
        return 2

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
