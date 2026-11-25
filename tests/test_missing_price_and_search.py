import csv
import json
from dataclasses import replace
from decimal import Decimal
from foodai.agents.basket import BasketAgent
from foodai.import_catalog import import_data
from foodai.services.catalog import Catalog
from foodai.services.llm import MockLLM


async def test_named_black_tea_quantity_is_not_part_of_product_name(catalog):
    template = catalog.products[('samokat:msk_center','msk_center','samokat_milk')]
    for sku, name in [('tea','Чай черный крупнолистовой 100 г'), ('green','Чай зеленый'), ('cleaner','Ополаскиватель Белый чай')]:
        p = replace(template, sku=sku, name=name, category='Напитки' if sku!='cleaner' else 'Бытовая химия',
                    package_qty=Decimal('100'), package_unit='g', price_rub=Decimal('100'))
        catalog.products[(p.storefront_id,p.zone_id,p.sku)] = p
        catalog.by_zone[p.zone_id].append(p)
    agent = BasketAgent(catalog,MockLLM())
    context = {'zone_id':'msk_center','basket_state':{'zone_id':'msk_center','draft':[
        {'id':'tea','query':'чай черный','quantity':None,'unit':None}]}}
    result = await agent.run('чай черный одна упаковка',context)
    assert context['basket_state']['draft'][0]['query']=='чай черный'
    assert context['basket_state']['draft'][0]['quantity']=='1'
    assert context['basket_state']['draft'][0]['unit']=='pack'
    assert any(line['sku']=='tea' for b in result.structured['comparison']['full'] for line in b['lines'])
    assert not any(line['sku'] in ('green','cleaner') for b in result.structured['comparison']['full'] for line in b['lines'])


async def test_natural_refusal_removes_pending_cola_and_preserves_milk(catalog):
    p = replace(catalog.products[('samokat:msk_center','msk_center','samokat_milk')],
                sku='cola',name='Coca-Cola Zero 330 мл',package_qty=Decimal('330'))
    catalog.products[(p.storefront_id,p.zone_id,p.sku)]=p
    catalog.by_zone[p.zone_id].append(p)
    agent=BasketAgent(catalog,MockLLM())
    context={'zone_id':'msk_center'}
    first=await agent.run('молоко 1 л и колу без сахара 1,5 л',context)
    assert first.status=='need_clarification' and context['basket_state']['pending_preference']['kind']=='package'
    result=await agent.run('не, не надо её',context)
    assert [r['query'] for r in context['basket_state']['draft']]==['молоко']
    assert 'Убрал из списка' in result.text and 'Можно взять' not in result.text


async def test_removing_last_pending_product_leaves_empty_cart(catalog):
    p = replace(catalog.products[('samokat:msk_center','msk_center','samokat_milk')],
                sku='cola',name='Coca-Cola Zero 330 мл',package_qty=Decimal('330'))
    catalog.products[(p.storefront_id,p.zone_id,p.sku)]=p
    catalog.by_zone[p.zone_id].append(p)
    agent=BasketAgent(catalog,MockLLM())
    context={'zone_id':'msk_center'}
    await agent.run('колу без сахара 1,5 л',context)
    result=await agent.run('не, не надо ее',context)
    assert result.status=='ok' and 'Корзина пуста' in result.text
    assert context['basket_state']['draft']==[] and context['_usage']['model_calls']==0


def test_unverified_price_is_diagnostic_only_and_never_enters_pricing(tmp_path):
    source=tmp_path/'input.csv'
    rows=[{'service':'ozon_fresh','zone_id':'msk_center','product_id':sku,'name':name,
           'price':'89','amount':'800 г','parsed_at':'2026-11-21 10:00:00','in_stock':'true',
           'extra':json.dumps({'ordinary_price_inferred':inferred})}
          for sku,name,inferred in [('1','Гречка Makfa',True),('2','Рис',False)]]
    with source.open('w',encoding='utf8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]))
        writer.writeheader();writer.writerows(rows)
    target=tmp_path/'catalog.json'
    import_data(source,target,kind='csv')
    catalog=Catalog(target)
    assert catalog.search_products('гречка',['msk_center'])=={}
    assert catalog.missing_reason('гречка','msk_center')=='unverified_ordinary_price'
    assert 'price' not in catalog.excluded_by_zone['msk_center'][0]
    assert catalog.get_product_details('1','ozon_fresh:address:msk_center','msk_center') is None


def test_conversation_words_are_removed_from_greek_salad_query(catalog):
    agent=BasketAgent(catalog,None)
    state={'draft':[{'id':'s','query':'салат','quantity':'2','unit':'pack'}]}
    result=agent._execute('set_requirements',{'items':[{'id':'s','query':'салат греческий хочу','quantity':None,'unit':None}],
                                            'replace_all':False},state,'msk_center',{})
    assert result['requirements'][0]['query']=='салат греческий'
    assert result['requirements'][0]['quantity']=='2'


async def test_saved_requirements_can_be_rebuilt_without_reentering_list(catalog):
    context={'zone_id':'msk_center','basket_state':{'zone_id':'msk_center','draft':[
        {'id':'milk','query':'молоко','quantity':'1000','unit':'ml'}]}}
    result=await BasketAgent(catalog,MockLLM()).run('покажи корзину',context)
    assert result.status=='ok' and result.structured['comparison']['full']
    assert context['_usage']['model_calls']==1
