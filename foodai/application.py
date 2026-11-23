import asyncio
from collections import defaultdict
from foodai.models import AgentResult

MODES = {'basket': '🛒 Где купить дешевле?', 'meal_plan': '🥗 Составить рацион',
         'cook': '🍳 Что приготовить?', 'savings': '💰 Сэкономить на покупках'}
PROMPTS = {'basket': 'Напишите, что хотите купить. Например: «Купи молоко и яйца». Если количества не хватает, я уточню.',
           'meal_plan': 'Напиши цель по КБЖУ, число дней и бюджет.',
           'cook': 'Напиши, что есть дома, сколько порций нужно и сколько времени есть на готовку.',
           'savings': 'Пришли свою корзину и напиши, что заменять нельзя.'}


class Application:
    def __init__(self, storage, addresses, agents):
        self.storage, self.addresses, self.agents = storage, addresses, agents
        self.locks = defaultdict(asyncio.Lock)

    async def handle(self, user_id, text='', location=None):
        async with self.locks[user_id]:
            state = self.storage.get(user_id)
            text = (text or '').strip()
            if text == '/help':
                return AgentResult('Выберите адрес дома кнопкой или отправьте адрес либо геопозицию, затем раздел меню. '
                    'Для корзины укажите продукты и количества. /menu — главное меню. '
                    'Цены берутся из сохранённых данных. Стоимость доставки не включена.', 'ok')
            if text in ('Изменить адрес', '/address'):
                state = {'mode': 'WAIT_ADDRESS', 'zone_id': None, 'history': []}
                self.storage.save(user_id, state)
                return AgentResult('Выберите адрес дома кнопкой ниже или пришлите адрес либо геопозицию.', 'need_clarification')
            if text in ('/start', '/menu', 'Главное меню', 'Назад'):
                state['mode'] = 'MAIN_MENU' if state.get('zone_id') else 'WAIT_ADDRESS'
                self.storage.save(user_id, state)
                address = self.addresses.zone_info(state['zone_id']) if state.get('zone_id') else {}
                return AgentResult(('📍 ' + (address['catalog_address'] or address['zone_name']) + '\nВыберите раздел.')
                                   if address else 'Выберите адрес дома кнопкой ниже или пришлите адрес либо геопозицию.', 'ok')
            if not state.get('zone_id'):
                resolved = self.addresses.resolve_address(address=text, **(location or {}))
                if resolved['status'] != 'resolved':
                    return AgentResult('Пока не удалось определить этот дом. Выберите адрес кнопкой ниже '
                                       'или отправьте геопозицию.', 'need_clarification')
                state.update(mode='MAIN_MENU', zone_id=resolved['zone_id'], address_source=resolved['source'],
                             address_value=location or text)
                self.storage.save(user_id, state)
                label = resolved['catalog_address'] or resolved['zone_name']
                notice = ('\nПо геопозиции выбрана зона «' + resolved['zone_name'] +
                          '». Расчёт будет для указанного дома.') if resolved['source'] == 'administrative_polygon' else ''
                return AgentResult('📍 Адрес для расчёта: ' + label + notice + '\nВыберите раздел.', 'ok')
            modes = {label: mode for mode, label in MODES.items()}
            if text in modes:
                state['mode'] = modes[text]
                self.storage.save(user_id, state)
                return AgentResult(PROMPTS[state['mode']], 'ok')
            if state['mode'] not in MODES:
                return AgentResult('Выбери раздел кнопкой в меню.', 'need_clarification')
            mode = state['mode']
            agent = self.agents.get(mode)
            if agent is None:
                return AgentResult('Агент пока подключается. ' + PROMPTS[mode], 'partial')
            if not text or len(text) > 4000:
                return AgentResult('Напиши текстовый запрос до 4 000 символов.', 'need_clarification')
            context = {'zone_id': state['zone_id'], 'basket_state': state.get('basket_state', {}),
                       **self.addresses.zone_info(state['zone_id']),
                       'history': state.get('history', []), 'last_result': state.get('last_result', {}),
                       'agent_state': state.get('agent_states', {}).get(mode, {}),
                       'catalog': self.addresses.catalog,
                       'storefronts': self.addresses.catalog.storefronts(state['zone_id'])}
            try:
                result = await agent.run(text, context) if hasattr(agent, 'run') else await agent(text, context)
                if isinstance(result, dict):
                    result = AgentResult(**result)
            except Exception:
                # Do not send stack traces, addresses, tokens or API bodies to Telegram.
                return AgentResult('Не удалось выполнить запрос. Попробуйте ещё раз или вернитесь в меню.', 'error')
            state['basket_state'] = context.get('basket_state', {})
            state.setdefault('agent_states', {})[mode] = context.get('agent_state', {})
            state['history'] = (state.get('history', []) + [{'user': text, 'status': result.status}])[-6:]
            structured = {k: v for k, v in result.structured.items() if k != 'tool_trace'}
            state['last_result'] = structured
            self.storage.save(user_id, state)
            return result
