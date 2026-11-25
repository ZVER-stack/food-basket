"""Collect the supplied ten administrative zones and export their exact template."""
import argparse
import csv
import json
from pathlib import Path
import sqlite3
from contextlib import closing

import ozon_fresh_fast as core
import ozon_live_parser as live
from ozon_refresh import inherit_access_state
from ozon_house_export import FIELDS, HEADERS
from ozon_runtime import system_awake
from run_parser import preflight

CDP = 'http://127.0.0.1:9222'
ROOT = Path(__file__).resolve().parent
COMPACT_HEADERS = ["Код зоны", *HEADERS[1:]]


def export_template(snapshot, zones_file, template_file, output):
    zones, skipped = core.load_zones(zones_file)
    if skipped or len(zones) != 10:
        raise core.ParseError('Expected ten verified administrative delivery zones')
    mapping = {z['zone_id']: z for z in zones}
    with Path(template_file).open(encoding='utf-8-sig', newline='') as stream:
        fields = next(csv.reader(stream))
    expected = COMPACT_HEADERS
    if fields != expected:
        raise core.ParseError('Administrative compact template must contain the supported 14 fields')
    snapshot = Path(snapshot)
    if not (snapshot/'snapshot.sqlite').exists():
        return None
    counts = {}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix('.csv.tmp')
    with closing(sqlite3.connect((snapshot/'snapshot.sqlite').resolve().as_uri()+'?mode=ro', uri=True)) as db:
        with temp.open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for zid, status, stamp, payload in db.execute(
                    "SELECT h3,status,parsed_at,payload FROM zones ORDER BY rowid"):
                if zid not in mapping:
                    raise core.ParseError('Unknown administrative zone in saved result')
                if status == 'no_delivery':
                    writer.writerow(dict(zip(fields[:2], (zid, mapping[zid]["address"]))))
                    counts[zid] = 0
                    continue
                if status not in ('complete', 'collected_with_gaps'):
                    continue
                rows = json.loads(payload)
                if len({str(r['product_id']) for r in rows}) != len(rows):
                    raise core.ParseError('Duplicate SKU in an administrative zone')
                for raw in rows:
                    row = {key: raw.get(key) for key in core.COLUMNS if key != "h3_index"}
                    row['service'] = 'ozon_fresh'
                    row['zone_id'] = zid
                    row['parsed_at'] = row['parsed_at'] or stamp
                    if any(row.get('zone_id' if key == 'h3_index' else key) in (None, '')
                           for key in core.REQUIRED):
                        raise core.ParseError('Administrative export lacks a required field')
                    values = [zid, mapping[zid]['address']] + [raw.get(key) for key in FIELDS]
                    writer.writerow(dict(zip(fields, values, strict=True)))
                counts[zid] = len(rows)
    temp.replace(output)
    report = dict(csv=str(output.resolve()), columns=len(fields), zones_exported=len(counts),
                  rows=sum(counts.values()), per_zone=counts)
    core.write_json(output.with_suffix('.coverage.json'), report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zones', required=True)
    parser.add_argument('--template', required=True)
    parser.add_argument('--out-dir', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--export-only', action='store_true')
    args = parser.parse_args(argv)
    zones, skipped = core.load_zones(args.zones)
    if skipped or len(zones) != 10 or any('zone_id' not in row for row in zones):
        parser.error('Use all ten administrative zones from the supplied archive')
    if args.export_only:
        print(json.dumps(export_template(args.out_dir, args.zones, args.template, args.output),
                         ensure_ascii=False))
        return 0
    output = Path(args.out_dir)
    output.mkdir(parents=True, exist_ok=True)
    inherit_access_state(output, ROOT/'results/full_shared_20261018_111443', ROOT/'results')
    state = core.load_json(output/'backoff_status.json') if (output/'backoff_status.json').exists() else {}
    guard_path = ROOT/'work/browser_transport_9222.json'
    guard = core.load_json(guard_path) if guard_path.exists() else {}
    if (state.get('manual_required_since_unix') or state.get('requires_manual_access_check') or
            (guard.get('blocked') and any(word in str(guard.get('reason')).casefold()
             for word in ('captcha', 'challenge', 'manual', 'authenticated', 'капч')))):
        raise core.ProtectionError('Fresh human access confirmation is required before resuming')
    ready = preflight(CDP, allow_duplicate_test_tabs=True)
    print('Открытая карточка доступна. Адрес: '+str(ready.get('address')), flush=True)
    namespace = argparse.Namespace(
        mode='full', cdp=CDP, zones=args.zones, categories=str(ROOT/'categories_live.json'),
        out_dir=args.out_dir, transport_state=str(guard_path), limit=0, interval=1.5,
        max_interval=6.0, http_error_steps=(2.5, 3.5, 4.5, 5.5, 6.0),
        http_error_statuses=(403, 429), seconds=7200, max_requests=1000000,
        retry_after_pause=False, enrich_pilot_prices=True, browser_page_url=ready['product_url'],
        price_workers=7, catalog_workers=7, price_policy='catalog_metadata_cache',
        reference_snapshot=None, metadata_store=None,
        allow_duplicate_test_tabs=True, continue_until_finished=True,
        wait_on_403=True, quarantine_incomplete=True, product_mode='api',
        active_pointer=str(output/'active_run.json'))
    try:
        with system_awake():
            return live.run(namespace)
    finally:
        result = export_template(args.out_dir, args.zones, args.template, args.output)
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
