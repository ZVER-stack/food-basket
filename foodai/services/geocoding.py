"""Small, cached, user-triggered Photon address lookup; no autocomplete or bulk scans."""
import asyncio
from contextlib import closing
import json
import re
import sqlite3
import time
from pathlib import Path
import httpx
from foodai.services.address import address_key, address_parts


class PhotonGeocoder:
    def __init__(self, cache_path, url='https://photon.komoot.io/api/'):
        self.url, self.path = url, str(cache_path)
        self.lock, self.last_request = asyncio.Lock(), 0
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS geocoding (query TEXT PRIMARY KEY, result TEXT)')

    async def lookup(self, address):
        # Send only a street/house, never apartment, entrance, floor or delivery instructions.
        query = re.split(r'\b(?:кв(?:артира)?|подъезд|этаж|домофон)\b', address, flags=re.I)[0].strip(' ,.')
        if len(query) > 220 or not re.search(r'\d', query):
            return []
        if not re.search(r'\bмосква\b', query, flags=re.I):
            query = 'Москва, ' + query
        query = re.sub(r'(?<=\d)\s*(?:корпус|корп\.?|к)\s*(?=\d)', ' к', query, flags=re.I)
        requested = address_parts(query)
        if not requested:
            return []
        key = 'v3:' + ' '.join(address_key(query))
        async with self.lock:
            with closing(sqlite3.connect(self.path)) as db, db:
                row = db.execute('SELECT result FROM geocoding WHERE query=?', (key,)).fetchone()
            if row and json.loads(row[0]):
                return json.loads(row[0])
            features = await self._search(query, limit=5)
            if features is None:
                return None
            results, suggestions = self._matches(features, requested)
            if not results:
                # Photon can rank 47 к2 above 2 к2. Broaden retrieval once, then
                # validate the ORIGINAL street, house and corpus in exact order.
                street = re.sub(r'\b(?:(?:г\.?|город)\s+)?москва\b', '', requested[0], flags=re.I).strip(' ,')
                if not re.search(r'\b(?:улица|ул|шоссе|проспект|проезд|переулок|набережная|бульвар|площадь)\b', street, re.I):
                    street = 'улица ' + street
                fallback = await self._search(f'{street} {requested[3][0]}', limit=50, dedupe=0)
                if fallback is not None:
                    results, more = self._matches(fallback, requested)
                    suggestions.extend(more)
                elif not suggestions:
                    return None
            if not results:
                results = list({r['address']: r for r in suggestions}.values())[:4]
            if results:
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute('INSERT OR REPLACE INTO geocoding VALUES (?,?)', (key, json.dumps(results, ensure_ascii=False)))
            return results

    async def _search(self, query, **params):
        await asyncio.sleep(max(0, 1 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(self.url, params={'q': query, 'bbox': '36.8,55.1,38.2,56.2', **params},
                    headers={'User-Agent': 'FoodAI/0.1 (https://github.com/ZVER-stack/food-basket)',
                             'Accept-Language': 'ru'})
                response.raise_for_status()
                features = response.json().get('features')
                return features if isinstance(features, list) else None
        except (httpx.HTTPError, ValueError, AttributeError):
            return None

    @staticmethod
    def _matches(features, requested):
        results, suggestions = [], []
        for feature in features:
            props = feature.get('properties', {})
            point = feature.get('geometry', {}).get('coordinates', [])
            street, house = props.get('street'), props.get('housenumber')
            if not street or not house or len(point) != 2:
                continue
            # The bounding box includes the Moscow region: do not relabel its
            # houses as Moscow just because their street and number coincide.
            if props.get('city') and props['city'] != 'Москва':
                continue
            if props.get('state') and props['state'] != 'Москва':
                continue
            canonical = f'Москва, {street}, {house}'
            candidate = address_parts(canonical)
            if not candidate or sorted(candidate[2]) != sorted(requested[2]):
                continue
            item = {'address': canonical, 'lon': point[0], 'lat': point[1]}
            if candidate[3] == requested[3]:
                results.append(item)
            elif candidate[3][0] == requested[3][0]:
                suggestions.append(item | {'requires_confirmation': True})
        return results, suggestions
