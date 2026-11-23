import asyncio
import json
import re
import httpx

from foodai.models import number
from foodai.services.units import normalize_quantity


class LLMError(Exception):
    pass


class GroqLLM:
    def __init__(self, api_key, model, base_url):
        self.api_key, self.model, self.base_url = api_key, model, base_url.rstrip('/')

    async def complete(self, messages, tools):
        if not self.api_key:
            raise LLMError('Не задан GROQ_API_KEY. Для офлайн-демо используйте LLM_MODE=mock.')
        async with httpx.AsyncClient(timeout=35) as client:
            for attempt in range(3):
                try:
                    response = await client.post(self.base_url + '/chat/completions',
                        headers={'Authorization': 'Bearer ' + self.api_key},
                        json={'model': self.model, 'messages': messages, 'tools': tools,
                              'tool_choice': 'auto', 'parallel_tool_calls': False,
                              'temperature': 0.1, 'max_completion_tokens': 3000})
                    if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                        try:
                            pause = min(5, max(1, float(response.headers.get('retry-after', attempt + 1))))
                        except ValueError:
                            pause = attempt + 1
                        await asyncio.sleep(pause)
                        continue
                    if response.status_code != 200:
                        raise LLMError(f'API модели недоступен (HTTP {response.status_code}). Попробуйте позже.')
                    return response.json()['choices'][0]['message']
                except (httpx.TimeoutException, httpx.TransportError):
                    if attempt == 2:
                        raise LLMError('Модель не ответила после трёх попыток.') from None
                    await asyncio.sleep(attempt + 1)
                except (KeyError, ValueError, IndexError):
                    raise LLMError('Модель вернула некорректный ответ.') from None


class MockLLM:
    """Deterministic offline fixture driver; this is explicitly not a live LLM."""
    async def complete(self, messages, tools):
        user_index = max(i for i, m in enumerate(messages) if m['role'] == 'user')
        recent = messages[user_index + 1:]
        state = json.loads(messages[1]['content'])
        tool_results = [m for m in recent if m['role'] == 'tool']
        def call(name, args):
            return {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': f'call_{len(tool_results)}', 'type': 'function',
                 'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}]}
        if not tool_results:
            text = messages[user_index]['content'].lower().replace('ё', 'е')
            old = state.get('requirements', [])
            if 'безлактоз' in text and old:
                milk = next((r for r in old if 'молок' in r['query']), None)
                if milk:
                    req = dict(milk, query='молоко безлактозное', required_terms=['безлактоз'])
                    return call('set_requirements', {'items': [req], 'replace_all': False})
            items = []
            for index, part in enumerate(re.split(r'[,;\n]+', text)):
                match = re.fullmatch(r'\s*(.+?)\s+(\d+(?:[.,]\d+)?)\s*(кг|г|мл|л|шт\.?)\s*', part)
                if not match:
                    return {'role': 'assistant', 'content': 'Укажите продукт и количество, например: молоко 1 л, яйца 10 шт.'}
                qty, unit = normalize_quantity(match[2], match[3].rstrip('.'))
                items.append({'id': f'item_{index}', 'query': match[1], 'quantity': str(qty),
                              'unit': unit, 'required_terms': [], 'exclude_terms': [], 'exclude_brands': []})
            return call('set_requirements', {'items': items, 'replace_all': True})
        result = json.loads(tool_results[-1]['content'])
        name = tool_results[-1]['name']
        if result.get('error'):
            return {'role': 'assistant', 'content': 'Не получилось обработать запрос. Уточните продукты и количества.'}
        if name == 'set_requirements':
            return call('search_products', {'queries': [{'requirement_id': r['id'], 'query': r['query']}
                                                       for r in result['requirements']], 'limit': 5})
        if name == 'search_products':
            candidates = {store: [] for store in state['storefronts']}
            reqs = {r['id']: r for r in state.get('requirements', [])}
            # The requirements from the current set tool are authoritative.
            for entry in tool_results:
                if entry['name'] == 'set_requirements':
                    reqs = {r['id']: r for r in json.loads(entry['content'])['requirements']}
            for key, stores in result['results'].items():
                for store, products in stores.items():
                    if not products:
                        continue
                    req = reqs[key]
                    def cost(p):
                        from decimal import ROUND_CEILING
                        return ((number(req['quantity']) / number(p['package_qty']))
                                .to_integral_value(rounding=ROUND_CEILING) * number(p['price_rub']))
                    best = min(products, key=cost)
                    candidates[store].append({'requirement_id': key, 'sku': best['sku']})
            return call('calculate_basket', {'baskets': [{'storefront_id': store, 'items': items}
                                                        for store, items in candidates.items()]})
        if name == 'calculate_basket':
            return call('compare_stores', {})
        return {'role': 'assistant', 'content': 'Расчёт завершён.'}
