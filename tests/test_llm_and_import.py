from datetime import datetime
import json
import sqlite3
import httpx
from unittest.mock import patch
import pytest
from foodai.import_catalog import convert, import_data
from foodai.services.llm import GroqLLM, LLMError
from foodai.agents.basket import TOOLS


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
    waits = []
    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={'Retry-After': '12'})
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'Уточните количество'}}]})
    real_client = httpx.AsyncClient
    async def no_wait(seconds):
        waits.append(seconds)
    monkeypatch.setattr('foodai.services.llm.asyncio.sleep', no_wait)
    with patch('foodai.services.llm.httpx.AsyncClient', side_effect=lambda **kw: real_client(
            transport=httpx.MockTransport(handle), **kw)):
        response = await GroqLLM('secret', 'openai/gpt-oss-120b', 'https://api.groq.com/openai/v1').complete(
            [{'role': 'user', 'content': 'Привет'}], [])
    assert len(requests) == 2 and response['content']
    assert waits == [12]
    assert json.loads(requests[-1].content)['parallel_tool_calls'] is False


async def test_tool_generation_400_has_one_strict_json_recovery_without_losing_nulls():
    requests = []
    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(400, json={'error': {'code': 'tool_use_failed',
                'message': 'Tool choice is required, but model did not call a tool'}})
        assert 'tools' not in payload and 'tool_choice' not in payload
        assert payload['response_format']['json_schema']['strict'] is True
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': json.dumps({
            'action': {'name': 'set_requirements', 'arguments': {'items': [{'id': 'milk', 'query': 'молоко',
                'quantity': None, 'unit': None, 'brand': None, 'required_terms': None,
                'exclude_terms': None, 'exclude_brands': None}], 'replace_all': True}}})}}]})
    real = httpx.AsyncClient
    with patch('foodai.services.llm.httpx.AsyncClient', side_effect=lambda **kw: real(**kw, transport=httpx.MockTransport(handle))):
        result = await GroqLLM('secret', 'openai/gpt-oss-120b', 'https://api.groq.com/openai/v1').complete(
            [{'role': 'user', 'content': 'молоко'}], TOOLS[:1])
    assert len(requests) == 2
    args = json.loads(result['tool_calls'][0]['function']['arguments'])
    assert args['items'] == [{'id': 'milk', 'query': 'молоко', 'quantity': None, 'unit': None}]


async def test_unrelated_http_400_is_not_retried_or_hidden_by_recovery():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(400, json={'error': {'code': 'invalid_request_error', 'message': 'Bad request'}})
    real = httpx.AsyncClient
    with patch('foodai.services.llm.httpx.AsyncClient', side_effect=lambda **kw: real(**kw, transport=httpx.MockTransport(handle))):
        with pytest.raises(LLMError, match='Корзина сохранена'):
            await GroqLLM('secret', 'openai/gpt-oss-120b', 'https://api.groq.com/openai/v1').complete(
                [{'role': 'user', 'content': 'молоко'}], TOOLS[:1])
    assert len(requests) == 1


def test_structured_recovery_cannot_execute_an_unoffered_action():
    with pytest.raises(ValueError):
        GroqLLM.decode_action({'content': '{"action":{"name":"run_python","arguments":{}}}'}, TOOLS, 'test')
