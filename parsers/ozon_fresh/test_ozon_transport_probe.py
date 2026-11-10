import tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import ozon_fresh_fast as core
from ozon_transport_probe import run_queued_transport_probe
from test_ozon_catalog_index import CAT,card,page


class TransportProbeChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.zone={'h3_index':'H','address':'Дом1'}
        core.write_json(self.root/'transport_probe_request.json',{
            'id':'comparison','h3_index':'H','category_id':CAT['category_id']})
    def test_only_two_fixed_public_routes_same_house_and_no_replacement(self):
        client=Mock();client.request.side_effect=[page([card('1')]),page([card('1'),card('2')])]
        location=Mock()
        with patch('ozon_har_catalog.address_title',return_value='Дом1'):
            run_queued_transport_probe(self.root,self.zone,[CAT],client,location)
        report=core.load_json(self.root/'transport_probes/comparison/report.json')
        self.assertEqual([c['items'] for c in report['cases']],[1,2])
        self.assertFalse(report['catalog_replacement_enabled'])
        urls=[c.args[0]['url'] for c in client.request.call_args_list]
        self.assertIn('/api/entrypoint-api.bx/page/json/v2?',urls[0])
        self.assertIn('/api/composer-api.bx/page/json/v2?',urls[1])
        self.assertEqual(location.confirm.call_count,2)
        run_queued_transport_probe(self.root,self.zone,[CAT],client,location)
        self.assertEqual(client.request.call_count,2)
    def test_protection_prevents_trying_another_transport(self):
        client=Mock();client.request.side_effect=core.ProtectionError('HTTP403')
        location=Mock()
        with self.assertRaises(core.ProtectionError):
            run_queued_transport_probe(self.root,self.zone,[CAT],client,location)
        self.assertEqual(client.request.call_count,1)
        self.assertEqual(location.confirm.call_count,1)
        self.assertFalse(core.load_json(self.root/'transport_probes/comparison/report.json')['complete'])
    def test_wrong_house_cannot_be_accepted_as_fast_transport(self):
        client=Mock();client.request.side_effect=[page([card('1')]),page([card('2')])]
        location=Mock()
        with patch('ozon_har_catalog.address_title',side_effect=['Дом1','Другойдом']):
            run_queued_transport_probe(self.root,self.zone,[CAT],client,location)
        cases=core.load_json(self.root/'transport_probes/comparison/report.json')['cases']
        self.assertFalse(cases[1]['accepted_same_house_fresh_catalog'])
        location.confirm.assert_called()
