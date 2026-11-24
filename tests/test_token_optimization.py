import json
from unittest.mock import patch
import httpx
from foodai.agents.basket import BasketAgent
from foodai.services.llm import GroqLLM


async def test_two_provider_calls_keep_prices_constraints_and_report_usage(catalog, tmp_path):
    requests = []
    def response(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            name = 'set_requirements'
            args = {'replace_all': True, 'items': [{'id': 'milk', 'query': 'молоко',
                'quantity': '1000', 'unit': 'ml', 'required_terms': ['безлактоз']}]}
        else:
            context = json.loads(payload['messages'][1]['content'])
            assert context['phase'] == 'selection'
            assert 'draft' not in context and 'recent_dialogue' not in context
            rows = context['candidates']['milk']['samokat:msk_center']
            products = [dict(zip(context['fields'], row)) for row in rows]
            assert all('безлактоз' in row['name'].lower() for row in products)
            assert not any(m['role'] == 'tool' for m in payload['messages'])
            name = 'calculate_basket'
            args = {'baskets': [{'storefront_id': 'samokat:msk_center', 'items': [
                {'requirement_id': 'milk', 'sku': products[0]['sku']}]}]}
        return httpx.Response(200, json={'usage': {'prompt_tokens': 100, 'completion_tokens': 20,
            'total_tokens': 120, 'prompt_tokens_details': {'cached_tokens': 25}}, 'choices': [{'message': {
            'role': 'assistant', 'tool_calls': [{'id': name, 'type': 'function', 'function': {
                'name': name, 'arguments': json.dumps(args)}}]}}]})
    log = tmp_path / 'usage.jsonl'
    real = httpx.AsyncClient
    with patch('foodai.services.llm.httpx.AsyncClient', side_effect=lambda **kw: real(**kw, transport=httpx.MockTransport(response))):
        agent = BasketAgent(catalog, GroqLLM('secret', 'openai/gpt-oss-120b', 'https://api.groq.com/openai/v1', log))
        context = {'zone_id': 'msk_center'}
        result = await agent.run('Молоко безлактозное, литр', context)
        assert result.status == 'ok' and len(requests) == 2
        assert result.structured['usage']['total_tokens'] == 240
        assert result.structured['usage']['cached_tokens'] == 50
        assert result.structured['usage']['http_calls'] == 2
        assert result.structured['usage']['usage_reported'] is True
        again = await agent.run('А денег сколько?', context)
        assert again.text == result.text and len(requests) == 2
        assert again.structured['usage']['http_calls'] == 0
    records = [json.loads(line) for line in log.read_text(encoding='utf8').splitlines()]
    assert len(records) == 2 and records[0]['request_id'] == records[1]['request_id']
    assert sum(row['usage']['total_tokens'] for row in records) == 240
    assert 'secret' not in log.read_text(encoding='utf8')
    assert 'messages' not in records[0] and 'headers' not in records[0]


async def test_explicit_list_needs_only_selection_and_free_text_keeps_model_parser(catalog):
    from unittest.mock import AsyncMock
    assert BasketAgent.explicit_list('Купи литр молока и 10 яиц')
    assert BasketAgent.explicit_list('Молоко безлактозное 1 л') is None
    assert BasketAgent.explicit_list('Купи молоко определённой марки 1 л') is None
    llm = AsyncMock()
    def choose(messages, tools):
        data = json.loads(messages[1]['content'])
        assert data['phase'] == 'selection'
        choices = []
        for req in data['requirements']:
            products = [dict(zip(data['fields'], row)) for row in data['candidates'][req['id']]['samokat:msk_center']]
            choices.append({'requirement_id': req['id'], 'sku': products[0]['sku']})
        return {'role': 'assistant', 'tool_calls': [{'id': 'select', 'type': 'function', 'function': {
            'name': 'calculate_basket', 'arguments': json.dumps({'baskets': [{'storefront_id': 'samokat:msk_center', 'items': choices}]})}}]}
    llm.complete.side_effect = choose
    agent = BasketAgent(catalog, llm)
    result = await agent.run('Купи литр молока и 10 яиц', {'zone_id': 'msk_center'})
    assert result.status == 'ok' and llm.complete.call_count == 1
    assert [(r['quantity'],r['unit']) for r in result.structured['requirements']] == [('1000', 'ml'), ('10', 'pcs')]
    assert any(step.get('local') for step in result.structured['tool_trace'] if step['tool'] == 'search_products')
