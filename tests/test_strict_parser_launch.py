import importlib.util
from pathlib import Path
from types import SimpleNamespace


path = Path(__file__).parents[1]/'parsers/ozon_fresh/ozon_admin_strict.py'
spec = importlib.util.spec_from_file_location('strict_admin_launcher', path)
strict = importlib.util.module_from_spec(spec)
spec.loader.exec_module(strict)


def test_full_pass_options_have_no_reference_or_partial_export(tmp_path):
    args = strict.build_options(tmp_path, tmp_path/'out', 'zones', 'categories', 'https://www.ozon.ru/product/1/')
    assert args.limit == 0 and args.continue_until_finished and args.wait_on_403
    assert args.price_policy == 'verified_each_address' and not args.quarantine_incomplete
    assert args.reference_snapshot is None and args.metadata_store is None
    assert args.price_workers == args.catalog_workers == 7


def test_strict_index_requires_pdp_even_if_other_zone_price_or_metadata_match():
    class Base:
        def __init__(self, **kwargs): self.options = kwargs
        def resolve(self, house, item, products): return dict(item)
        def remember_product(self, house, row): return row
    module = SimpleNamespace(CatalogIndex=Base, extra_of=lambda row: row.get('extra', {}),
                             matching_quote=lambda *args: True, METADATA_FIELDS=('brand',))
    Index = strict.strict_index_type(module, SimpleNamespace(ParseError=ValueError))
    index = Index(allow_price_reuse=True, metadata_strategy=False)
    assert not index.options['allow_price_reuse'] and index.options['metadata_strategy']
    item = {'product_id':'1','extra':{'price_cache_match':'old cache'}}
    assert index.resolve('new_house', item, {})['extra']['product_details_required']
    import pytest
    with pytest.raises(ValueError, match='current-house'):
        index.remember_product('new_house', {'extra':{'ordinary_price_inferred':True}})
