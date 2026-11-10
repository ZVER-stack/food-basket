import contextlib,csv,io,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import ozon_fresh_fast as core
from ozon_product_prices import product_metadata
from test_ozon_fresh_fast import CARD,MAPPING,CAT,ZONE

class NutritionPrecisionTests(unittest.TestCase):
    literal='8.1999999999999993'
    def test_long_decimal_from_primary_nutrition_is_preserved_exactly(self):
        metadata,_=product_metadata({'widgetStates':{'webNutritionInfo-test':{'values':[
            {'label':'белки','value':self.literal},{'label':'жиры','value':'0'}]}}})
        self.assertEqual(metadata['protein'],self.literal);self.assertEqual(metadata['fat'],'0')
    def test_long_decimal_characteristic_is_preserved_exactly(self):
        metadata,raw=product_metadata({'widgetStates':{'webCharacteristics-test':{'items':[
            {'title':'Белки, г','values':[{'text':self.literal.replace('.',',')}]}]}}})
        self.assertEqual(metadata['protein'],self.literal)
    def test_full37_column_row_and_validation_accept_the_same_nutrition(self):
        row=core.card_row(CARD|{'protein':self.literal},MAPPING|{'protein':'/protein'},CAT)
        self.assertEqual(row['protein'],self.literal)
        self.assertEqual((row['price'],row['price_card'],row['price_regular']),('89.90','85','109'))
        row.update(service='ozon_fresh',h3_index=ZONE['h3_index'],parsed_at='2026-11-05 22:00:00')
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            file=Path(temp)/'products.csv'
            with file.open('w',encoding='utf-8-sig',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=core.COLUMNS);writer.writeheader();writer.writerow(row)
            with patch.object(core,'load_zones',return_value=([ZONE],[])),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(core.validate(SimpleNamespace(zones='fixture',csv=str(file),out=None)),0)
    def test_price_precision_limit_is_unchanged(self):
        with self.assertRaises(core.ParseError):core.number(self.literal)
        with self.assertRaises(core.ParseError):core.card_row(CARD|{'ordinary':self.literal},MAPPING,CAT)
    def test_invalid_negative_nonfinite_and_unbounded_nutrition_remain_rejected(self):
        for value in ('-1','NaN','Infinity',True,'1e99','1e-33','8-10'):
            with self.subTest(value=value),self.assertRaises(core.ParseError):core.nutrition_number(value)

if __name__=='__main__':unittest.main()
