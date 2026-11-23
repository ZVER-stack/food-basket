from datetime import datetime
import json
import sqlite3
import httpx
from unittest.mock import patch
import pytest
from foodai.import_catalog import convert, import_data
from foodai.services.llm import GroqLLM, LLMError


def test_import_keeps_verified_price_and_skips_inferred():
    row = {'product_id': '1', 'name': 'Молоко', 'amount': '0.93 л', 'price': '90',
           'in_stock': 'true', 'extra': {}, 'parsed_at': '2026-11-21 10:00:00'}
    product = convert(row, 'ozon_fresh', 'msk_center', row['parsed_at'], 'ordinary')
    assert product['package_qty'] == '930.00' and product['price_rub'] == '90'
    assert product['storefront_id'] == 'ozon_fresh:address:msk_center'
    with pytest.raises(ValueError):
        convert(row | {'extra': {'ordinary_price_inferred': True}}, 'ozon_fresh', 'msk_center', row['parsed_at'], 'ordinary')
    with pytest.raises(ValueError):
        convert(row, 'ozon_fresh', '8711aa123ffffff', row['parsed_at'], 'ordinary')


async def test_groq_uses_local_tools_and_retries_without_secret_leak(monkeypatch):
    requests = []
    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={'Retry-After': '0'})
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'Уточните количество'}}]})
    real_client = httpx.AsyncClient
    async def no_wait(seconds):
        pass
    monkeypatch.setattr('foodai.services.llm.asyncio.sleep', no_wait)
    with patch('foodai.services.llm.httpx.AsyncClient', side_effect=lambda **kw: real_client(
            transport=httpx.MockTransport(handle), **kw)):
        response = await GroqLLM('secret', 'openai/gpt-oss-120b', 'https://api.groq.com/openai/v1').complete(
            [{'role': 'user', 'content': 'Привет'}], [])
    assert len(requests) == 2 and response['content']
    assert json.loads(requests[-1].content)['parallel_tool_calls'] is False
