"""Replaceable address adapter. Only the supplied ten zone IDs are accepted."""
import csv
import json
import re
from pathlib import Path
from foodai.services.catalog import ZONE_IDS

ZONE_NAMES = ['Центр', 'Северо-восток', 'Восток', 'Юго-восток', 'Юг — восточная часть',
              'Юг — западная часть', 'Юго-запад', 'Запад', 'Северо-запад', 'Север']


def address_key(text):
    text = text.casefold().replace('ё', 'е')
    text = re.sub(r'(?<=\d)к(?=\d)', ' корпус ', text)
    text = re.sub(r'\b(?:корпус|корп\.?|к\.?)\s*(?=\d)', ' корпус ', text)
    text = re.sub(r'\b(?:строение|стр\.?)\s*(?=\d)', ' строение ', text)
    text = re.sub(r'\b(?:г\.?|город)\s*(?=москва\b)', ' ', text)
    text = re.sub(r'\b(?:москва|улица|ул)\b\.?', ' ', text)
    text = re.sub(r'\b(?:дом|д\.?)\s*(?=\d)', ' ', text)
    text = re.sub(r'\bпр-т\b', 'проспект', text)
    # Keep slash house numbers distinct from separate numbers and building suffixes.
    return tuple(re.findall(r'[а-яa-z]+|\d+(?:/\d+)?', text))


def in_ring(x, y, ring):
    inside = False
    for a, b in zip(ring, ring[1:] + ring[:1]):
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            inside = not inside
    return inside


class AddressService:
    def __init__(self, catalog, zones_path='', geojson_path='', geocoder=None):
        self.catalog = catalog
        self.geocoder = geocoder
        self.addresses = {}
        if zones_path:
            with Path(zones_path).open(encoding='utf-8-sig', newline='') as stream:
                self.addresses = {row['zone_id']: row for row in csv.DictReader(stream)}
            if set(self.addresses) != set(ZONE_IDS):
                raise ValueError('Адресный файл должен содержать десять зон проекта')
        self.geo = json.loads(Path(geojson_path).read_text(encoding='utf-8-sig')) if geojson_path else None

    def zone_info(self, zone):
        row = self.addresses.get(zone, {})
        return {'zone_name': row.get('zone_name') or dict(zip(ZONE_IDS, ZONE_NAMES)).get(zone, 'Выбранная зона'),
                'catalog_address': row.get('address', '')}

    def zone_label(self, zone):
        info = self.zone_info(zone)
        return info['catalog_address'] or info['zone_name']

    async def resolve_user_address(self, address=None, lat=None, lon=None):
        result = self.resolve_address(address, lat, lon)
        if result['status'] == 'resolved' or not address or not self.geocoder:
            return result
        matches = await self.geocoder.lookup(address)
        if matches is None:
            return {'status': 'unavailable', 'zone_id': None}
        if not matches:
            return {'status': 'not_found', 'zone_id': None}
        resolved = []
        for match in matches:
            zone = self.resolve_address(lat=match['lat'], lon=match['lon'])
            if zone['status'] == 'resolved':
                resolved.append(zone | {'user_address': match['address'], 'source': 'geocoded_address'})
        unique = {(r['zone_id'], r['user_address']): r for r in resolved}
        if len(unique) == 1:
            return next(iter(unique.values()))
        return {'status': 'ambiguous' if unique else 'unsupported', 'zone_id': None}

    def resolve_address(self, address=None, lat=None, lon=None):
        source, zone = '', None
        if address:
            literal = address.strip()
            if literal in ZONE_IDS:
                zone, source = literal, 'selected_zone'
            else:
                normalized = address_key(literal)
                hits = [key for key, row in self.addresses.items() if address_key(row['address']) == normalized]
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
                **self.zone_info(zone),
                'storefronts': self.catalog.storefronts(zone),
                'coverage_basis': 'representative_address_snapshot',
                'notice': 'Каталог снят по опорному адресу зоны. Доставка по другому дому не подтверждена.'}
