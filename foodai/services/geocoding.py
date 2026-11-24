"""Small, cached, user-triggered Photon address lookup; no autocomplete or bulk scans."""
import asyncio
from contextlib import closing
import json
import re
import sqlite3
import time
from pathlib import Path
import httpx
from foodai.services.address import address_key


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
        if 'москв' not in query.casefold():
            query = 'Москва, ' + query
        key = query.casefold().replace('ё', 'е')
        async with self.lock:
            with closing(sqlite3.connect(self.path)) as db, db:
                row = db.execute('SELECT result FROM geocoding WHERE query=?', (key,)).fetchone()
            if row:
                return json.loads(row[0])
            await asyncio.sleep(max(0, 1 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.get(self.url, params={'q': query, 'limit': 5,
                        'bbox': '36.8,55.1,38.2,56.2'},
                        headers={'User-Agent': 'FoodAI/0.1 (https://github.com/ZVER-stack/food-basket)',
                                 'Accept-Language': 'ru'})
                    response.raise_for_status()
                    features = response.json().get('features', [])
            except (httpx.HTTPError, ValueError):
                return None
            results = []
            for feature in features:
                props = feature.get('properties', {})
                point = feature.get('geometry', {}).get('coordinates', [])
                street, house = props.get('street'), props.get('housenumber')
                if not street or not house or len(point) != 2:
                    continue
                # Never accept a nearby different house as the requested house.
                requested_numbers = re.findall(r'\d+(?:/\d+)?', query)
                house_number = re.match(r'\d+(?:/\d+)?', house)
                if not house_number or house_number[0] not in requested_numbers:
                    continue
                canonical = f'Москва, {street}, {house}'
                if sorted(address_key(query)) != sorted(address_key(canonical)):
                    continue
                results.append({'address': canonical, 'lon': point[0], 'lat': point[1]})
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute('INSERT OR REPLACE INTO geocoding VALUES (?,?)', (key, json.dumps(results, ensure_ascii=False)))
            return results
