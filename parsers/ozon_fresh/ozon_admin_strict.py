"""Full, fresh ten-zone pass using the installed parser; no cross-zone price reuse."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys


def build_options(root, out, zones, categories, product_url, *, defer_incomplete=False, requested_interval=None):
    return argparse.Namespace(
        mode='full', cdp='http://127.0.0.1:9222', zones=str(zones), categories=str(categories),
        out_dir=str(out), transport_state=str(root/'work/browser_transport_9222.json'),
        limit=0, interval=requested_interval or 1.5, explicit_requested_interval=requested_interval is not None, max_interval=6.0, http_error_steps=(2.5,3.5,4.5,5.5,6.0),
        http_error_statuses=(403,429), seconds=7200, max_requests=1000000,
        retry_after_pause=False, enrich_pilot_prices=True, browser_page_url=product_url,
        price_workers=7, catalog_workers=7, price_policy='verified_each_address',
        reference_snapshot=None, metadata_store=None, allow_duplicate_test_tabs=True,
        continue_until_finished=True, wait_on_403=True, quarantine_incomplete=defer_incomplete,
        product_mode='api', active_pointer=str(out/'active_run.json'))


def strict_index_type(module, core):
    class StrictCatalogIndex(module.CatalogIndex):
        def __init__(self, *args, **kwargs):
            kwargs.update(allow_price_reuse=False, metadata_strategy=True,
                          reference_snapshot=None, metadata_store=None)
            super().__init__(*args, **kwargs)

        def resolve(self, house, item, products):
            result = super().resolve(house, item, products)
            previous = products.get(item['product_id'])
            old = module.extra_of(previous) if previous else {}
            if (previous and old.get('ordinary_price_verified_for_address') is True
                    and not old.get('ordinary_price_inferred')
                    and old.get('product_metadata_schema') == 2
                    and module.matching_quote(result, previous)):
                result = module.reuse_quote(result, previous, house, house)
                for field in module.METADATA_FIELDS:
                    if previous.get(field) not in (None, ''):
                        result[field] = deepcopy(previous[field])
                extra = module.extra_of(result)
                for field in ('product_metadata_schema', 'product_metadata_observed_at',
                              'product_characteristics', 'product_metadata_fields_observed'):
                    if field in old:
                        extra[field] = deepcopy(old[field])
                extra['product_details_required'] = False
            else:
                extra = module.extra_of(result)
                extra['product_details_required'] = True
            result['extra'] = extra
            return result

        def remember_product(self, house, row):
            extra = module.extra_of(row)
            if (extra.get('ordinary_price_inferred')
                    or extra.get('ordinary_price_verified_for_address') is not True
                    or extra.get('product_metadata_schema') != 2):
                raise core.ParseError('Strict pass requires current-house PDP price and checked product metadata')
            return super().remember_product(house, row)
    return StrictCatalogIndex


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parser-root', required=True)
    parser.add_argument('--zones', required=True)
    parser.add_argument('--categories', required=True)
    parser.add_argument('--template', required=True)
    parser.add_argument('--out-dir', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--previous-snapshot', required=True)
    parser.add_argument('--defer-incomplete', action='store_true', help='Retain unresolved products durably and continue; never declare a gapped zone complete')
    parser.add_argument('--requested-interval', type=float, choices=(1.5,), help='Apply the fresh human request for a shorter pause once')
    args = parser.parse_args(argv)
    root, out = Path(args.parser_root).resolve(), Path(args.out_dir).resolve()
    sys.path.insert(0, str(root))
    import ozon_fresh_fast as core
    import ozon_live_parser as live
    import ozon_catalog_index as index
    from ozon_admin_zones import export_template
    from ozon_refresh import inherit_access_state
    from ozon_runtime import system_awake
    from run_parser import preflight
    zones, skipped = core.load_zones(args.zones)
    expected = {'msk_center', *(f'msk_{i:02}' for i in range(1,10))}
    if skipped or {z['h3_index'] for z in zones} != expected or len(zones) != 10:
        parser.error('All ten verified administrative zones are required')
    fingerprint = hashlib.sha256(Path(args.zones).read_bytes()+Path(args.categories).read_bytes()).hexdigest()
    manifest = dict(version=1, policy='fresh_pdp_each_zone', fingerprint=fingerprint,
                    price_reuse_between_zones=False, incomplete_zone_export=False)
    marker = out/'strict_policy.json'
    if (out/'snapshot.sqlite').exists() and (not marker.exists() or core.load_json(marker) != manifest):
        parser.error('Existing snapshot does not belong to this strict pass')
    out.mkdir(parents=True, exist_ok=True)
    core.write_json(marker, manifest)
    inherit_access_state(out, Path(args.previous_snapshot), root/'results')
    state = core.load_json(out/'backoff_status.json') if (out/'backoff_status.json').exists() else {}
    guard_path = root/'work/browser_transport_9222.json'
    guard = core.load_json(guard_path) if guard_path.exists() else {}
    if (state.get('manual_required_since_unix') or state.get('requires_manual_access_check')
            or guard.get('blocked')):
        raise core.ProtectionError('Saved access restriction must be resolved by a fresh human check')
    ready = preflight('http://127.0.0.1:9222', allow_duplicate_test_tabs=True)
    print(json.dumps({'preflight':ready['status'], 'sku':ready['product_id'], 'prices':ready['prices'],
                      'strict_policy':manifest, 'categories':len(core.load_json(args.categories))}, ensure_ascii=False), flush=True)
    original = index.CatalogIndex
    index.CatalogIndex = strict_index_type(index, core)
    try:
        with system_awake():
            return live.run(build_options(root, out, args.zones, args.categories, ready['product_url'],
                defer_incomplete=args.defer_incomplete, requested_interval=args.requested_interval))
    finally:
        index.CatalogIndex = original
        report = export_template(out, args.zones, args.template, args.output)
        print(json.dumps({'export':report}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
