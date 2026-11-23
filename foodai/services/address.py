"""Replaceable address adapter. Only the supplied ten zone IDs are accepted."""
import csv
import json
from pathlib import Path
from foodai.services.catalog import ZONE_IDS, words

ZONE_NAMES = ['Центр', 'Северо-восток', 'Восток', 'Юго-восток', 'Юг — восточная часть',
              'Юг — западная часть', 'Юго-запад', 'Запад', 'Северо-запад', 'Север']


def in_ring(x, y, ring):
    inside = False
    for a, b in zip(ring, ring[1:] + ring[:1]):
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            inside = not inside
    return inside


class AddressService:
    def __init__(self, catalog, zones_path='', geojson_path=''):
        self.catalog = catalog
        self.addresses = {}
        if zones_path:
            with Path(zones_path).open(encoding='utf-8-sig', newline='') as stream:
                self.addresses = {row['zone_id']: row for row in csv.DictReader(stream)}
            if set(self.addresses) != set(ZONE_IDS):
                raise ValueError('Адресный файл должен содержать десять зон проекта')
        self.geo = json.loads(Path(geojson_path).read_text(encoding='utf-8-sig')) if geojson_path else None

    def resolve_address(self, address=None, lat=None, lon=None):
        source, zone = '', None
        if address:
            literal = address.strip()
            if literal in ZONE_IDS:
                zone, source = literal, 'selected_zone'
            else:
                normalized = words(literal)
                hits = [key for key, row in self.addresses.items() if words(row['address']) == normalized]
                if len(hits) == 1:
                    zone, source = hits[0], 'verified_address_dictionary'
        elif lat is not None and lon is not None and self.geo:
            matches = []
            for feature in self.geo['features']:
                geometry = feature['geometry']
                polygons = [geometry['coordinates']] if geometry['type'] == 'Polygon' else geometry['coordinates']
                if any(in_ring(lon, lat, polygon[0]) and not any(in_ring(lon, lat, hole) for hole in polygon[1:])
                       for polygon in polygons):
                    matches.append(feature['properties']['zone_id'])
            if len(matches) == 1:
                zone, source = matches[0], 'administrative_polygon'
        if zone not in ZONE_IDS or not self.catalog.storefronts(zone):
            return {'status': 'unsupported', 'zone_id': None, 'storefronts': []}
        return {'status': 'resolved', 'zone_id': zone, 'source': source,
                'storefronts': self.catalog.storefronts(zone),
                'coverage_basis': 'representative_address_snapshot',
                'notice': 'Каталог снят по опорному адресу зоны. Доставка по другому дому не подтверждена.'}
