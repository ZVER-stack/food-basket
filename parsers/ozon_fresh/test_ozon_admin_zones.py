import csv
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

import ozon_fresh_fast as core
from ozon_admin_zones import COMPACT_HEADERS, export_template


class AdminZoneTests(unittest.TestCase):
    def test_ten_administrative_ids_and_template_export(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            zones = root/'zones.csv'
            ids = ['msk_center'] + [f'msk_{i:02}' for i in range(1, 10)]
            with zones.open('w', newline='', encoding='utf8') as stream:
                writer = csv.writer(stream)
                writer.writerow(['zone_id', 'zone_name', 'address', 'address_lat',
                                 'address_lon', 'address_in_zone'])
                writer.writerows((zid, zid, 'House', 55.7, 37.6, 'True') for zid in ids)
            loaded, skipped = core.load_zones(zones)
            self.assertFalse(skipped)
            self.assertEqual([z['h3_index'] for z in loaded], ids)
            template = root/'template.csv'
            fields = COMPACT_HEADERS
            template.write_text(','.join(fields)+'\n', encoding='utf8')
            row = dict(product_id='123', name='Milk',
                       amount='1 л', price='99', price_regular='110',
                       parsed_at='2026-11-21 12:00:00', extra={})
            db = sqlite3.connect(root/'snapshot.sqlite')
            try:
                db.execute('CREATE TABLE zones(h3,status,parsed_at,payload)')
                db.execute('INSERT INTO zones VALUES(?,?,?,?)',
                           (ids[0], 'complete', row['parsed_at'], json.dumps([row])))
                db.commit()
            finally:
                db.close()
            target = root/'out.csv'
            result = export_template(root, zones, template, target)
            self.assertEqual(result['columns'], 14)
            with target.open(encoding='utf8', newline='') as stream:
                data = list(csv.DictReader(stream))
            self.assertEqual(data[0]['Код зоны'], 'msk_center')
            self.assertEqual(data[0]['Название товара'], 'Milk')
            self.assertNotIn('h3_index', data[0])
            text = zones.read_text(encoding='utf8').replace('msk_01', 'msk_center')
            zones.write_text(text, encoding='utf8')
            with self.assertRaises(core.ParseError):
                core.load_zones(zones)
