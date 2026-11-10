"""Address invariants checked against the actual successful live modal workflow."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import ozon_fresh_fast as core
import ozon_har_catalog as tiles
from ozon_browser_transport import BrowserTransport
from ozon_live_parser import FreshLocation

FIXTURE = core.load_json(Path(__file__).parent/"testdata/live_address_protocol.json")
ZONE = {"h3_index": "8711aa632ffffff", "address": "Москва, Бакунинская улица, 60/2", "address_lat": "55.777199", "address_lon": "37.690003"}


def client():
    result = Mock()
    result.entrypoint.side_effect = [copy.deepcopy(FIXTURE[k]) for k in ("initial", "house", "deliver", "save", "root")]
    return result


class AddressProtocolTests(unittest.TestCase):
    def test_mkad_kilometer_abbreviation_preserves_house_building_and_coordinates(self):
        zone = dict(ZONE, address='Москва, "МКАД, 14-й километр", 2 с1')
        title = 'МКАД, 14-й км, 2 стр. 1'
        self.assertEqual(tiles.normalized_address(zone['address']), tiles.normalized_address(title))
        for other in ('МКАД, 15-й км, 2 стр. 1', 'МКАД, 14-й км, 3 стр. 1',
                      'МКАД, 14-й км, 2 стр. 2', 'МКАД, 14-й км, 2 корп. 1',
                      'МКАД, 14-й км, 2а стр. 1'):
            with self.subTest(other=other):
                self.assertNotEqual(tiles.normalized_address(zone['address']), tiles.normalized_address(other))
        for offset in (0, 0.02):
            root = copy.deepcopy(FIXTURE['root'])
            root['location']['current']['geoCoordinate'] = {'latitude':float(zone['address_lat'])+offset, 'longitude':float(zone['address_lon'])}
            tiles.widget(root, 'addressBookBarWeb-')['customCell']['cells'][0]['text']['text'] = title
            c = Mock(); c.entrypoint.return_value = root
            location = FreshLocation(c); location.current_zone = zone; location.delivery_available = True
            if offset:
                with self.assertRaises(core.ParseError): location.confirm()
            else: self.assertEqual(location.confirm()['verified_address'], title)

    def test_address_confirmation_uses_verified_fresh_category_instead_of_missing_home(self):
        category = {'category_id':'9201000','scope':'food',
                    'url':'https://www.ozon.ru/category/supermarket-ovoshchi-frukty-zelen-9201000/?miniapp=supermarket'}
        c = Mock(); c.entrypoint.return_value = copy.deepcopy(FIXTURE['root'])
        location = FreshLocation(c, confirmation_category=category)
        location.current_zone = ZONE; location.delivery_available = True
        self.assertEqual(location.confirm()['verified_address'], tiles.address_title(FIXTURE['root']))
        c.entrypoint.assert_called_once_with('/category/supermarket-ovoshchi-frukty-zelen-9201000/?miniapp=supermarket')
        for change in ('house', 'coordinates'):
            root = copy.deepcopy(FIXTURE['root'])
            if change == 'house':
                tiles.widget(root, 'addressBookBarWeb-')['customCell']['cells'][0]['text']['text'] = 'Другая улица, 1'
            else:
                root['location']['current']['geoCoordinate']['latitude'] = float(root['location']['current']['geoCoordinate']['latitude']) + 0.02
            c.entrypoint.return_value = root
            with self.subTest(change=change), self.assertRaises(core.ParseError): location.confirm()

    def test_address_confirmation_rejects_category_without_explicit_fresh_scope(self):
        for category in ({'scope':'food','url':'https://www.ozon.ru/category/milk-1/'},
                         {'scope':'other','url':'https://www.ozon.ru/category/milk-1/?miniapp=supermarket'},
                         {'scope':'food','url':'https://www.ozon.ru/my/orders?miniapp=supermarket'}):
            with self.subTest(category=category), self.assertRaises(core.ParseError):
                FreshLocation(Mock(), confirmation_category=category)

    def test_alley_abbreviation_before_comma_confirms_only_exact_house_and_coordinates(self):
        zone = dict(ZONE, address='Москва, Главная аллея, 11')
        title = 'Москва, Главная ал., 11'
        self.assertEqual(tiles.normalized_address(zone['address']), tiles.normalized_address(title))
        for other in ('Главная ал., 11а', 'Главная ал., 12', 'Главная ул., 11'):
            self.assertNotEqual(tiles.normalized_address(zone['address']), tiles.normalized_address(other))
        for offset in (0, 0.02):
            root = copy.deepcopy(FIXTURE['root'])
            root['location']['current']['geoCoordinate'] = {'latitude':float(zone['address_lat'])+offset, 'longitude':float(zone['address_lon'])}
            tiles.widget(root, 'addressBookBarWeb-')['customCell']['cells'][0]['text']['text'] = title
            c = Mock(); c.entrypoint.return_value = root
            location = FreshLocation(c); location.current_zone = zone; location.delivery_available = True
            if offset:
                with self.assertRaises(core.ParseError): location.confirm()
            else:
                self.assertEqual(location.confirm()['verified_address'], title)

    def test_reported_alley_alias_keeps_exact_house_corpus_and_street(self):
        expected = tiles.normalized_address('Москва, аллея Жемчуговой, 5 к4')
        self.assertEqual(expected, tiles.normalized_address('ал. Жемчуговой, 5 к. 4'))
        for other in ('ал. Жемчуговой, 5 к. 3', 'ал. Жемчуговой, 6 к. 4',
                      'ал. Жемчуговой, 5 стр. 4', 'ал. Жемчуговой, 5/1 к. 4',
                      'ул. Жемчуговой, 5 к. 4', 'ал. Другой, 5 к. 4',
                      'александровская Жемчуговой, 5 к. 4'):
            with self.subTest(other=other):
                self.assertNotEqual(expected, tiles.normalized_address(other))

    def test_reported_alley_header_requires_matching_coordinates(self):
        zone = dict(ZONE, address='Москва, аллея Жемчуговой, 5 к4')
        for latitude_offset in (0, 0.02):
            root = copy.deepcopy(FIXTURE['root'])
            root['location']['current']['geoCoordinate'] = {
                'latitude':float(zone['address_lat'])+latitude_offset,
                'longitude':float(zone['address_lon'])}
            tiles.widget(root, 'addressBookBarWeb-')['customCell']['cells'][0]['text']['text'] = 'ал. Жемчуговой, 5 к. 4'
            c = Mock(); c.entrypoint.return_value = root
            location = FreshLocation(c); location.current_zone = zone; location.delivery_available = True
            if latitude_offset:
                with self.assertRaises(core.ParseError): location.confirm()
            else:
                self.assertEqual(location.confirm()['verified_address'], 'ал. Жемчуговой, 5 к. 4')

    def test_reported_prospekt_abbreviation_confirms_the_exact_house_and_building(self):
        reported = core.load_json(Path(__file__).parent/'testdata/actual_prospekt_header_20261028.json')
        self.assertFalse(reported['synthetic'])
        zone = reported['zone']
        self.assertEqual(tiles.normalized_address(zone['address']), tiles.normalized_address(reported['reported_title']))
        # Synthetic root using the actual reported header and input coordinates.
        root = copy.deepcopy(FIXTURE['root'])
        root['location']['current']['geoCoordinate'] = {'latitude':float(zone['address_lat']), 'longitude':float(zone['address_lon'])}
        tiles.widget(root, 'addressBookBarWeb-')['customCell']['cells'][0]['text']['text'] = reported['reported_title']
        c = Mock(); c.entrypoint.return_value = root
        location = FreshLocation(c); location.current_zone = zone; location.delivery_available = True
        self.assertEqual(location.confirm()['verified_address'], reported['reported_title'])

    def test_prospekt_abbreviation_preserves_house_building_and_unambiguous_street_type(self):
        expected = tiles.normalized_address('Москва, проспект Мира, 102 с30')
        for other in ('пр-кт Мира, 102 стр. 31', 'пр-кт Мира, 102 корп. 30',
                      'пр-кт Мира, 101 стр. 30', 'пр-кт Мира, 102/1 стр. 30',
                      'пр-д Мира, 102 стр. 30', 'пр. Мира, 102 стр. 30',
                      'пр-к Мира, 102 стр. 30', 'пр-кт Другой, 102 стр. 30'):
            with self.subTest(other=other):self.assertNotEqual(expected, tiles.normalized_address(other))

    def test_matching_prospekt_header_does_not_override_wrong_coordinates(self):
        reported = core.load_json(Path(__file__).parent/'testdata/actual_prospekt_header_20261028.json')
        root = copy.deepcopy(FIXTURE['root'])
        root['location']['current']['geoCoordinate'] = {'latitude':float(reported['zone']['address_lat'])+0.02,
                                                      'longitude':float(reported['zone']['address_lon'])}
        tiles.widget(root, 'addressBookBarWeb-')['customCell']['cells'][0]['text']['text'] = reported['reported_title']
        c = Mock(); c.entrypoint.return_value = root
        location = FreshLocation(c); location.current_zone = reported['zone']; location.delivery_available = True
        with self.assertRaises(core.ParseError):location.confirm()

    def test_actual_drive_abbreviation_and_exact_coordinates_confirm_the_sixth_house(self):
        data=core.load_json(Path(__file__).parent/'testdata/actual_red_guard_address.json')
        self.assertFalse(data['synthetic'])
        zone=dict(h3_index='8711aa7a1ffffff',address='Москва, 1-й Красногвардейский проезд, 21 с2',
            address_lat='55.74937',address_lon='37.534073')
        c=Mock();c.entrypoint.return_value=data
        location=FreshLocation(c);location.current_zone=zone;location.delivery_available=True
        result=location.confirm()
        self.assertEqual(result['verified_address'],'1-й Красногвардейский пр-д, 21 стр. 2')
        c.entrypoint.assert_called_once()

    def test_drive_alias_does_not_discard_street_ordinal_building_or_street_type(self):
        expected=tiles.normalized_address('Москва, 1-й Красногвардейский проезд, 21 с2')
        for different in ('2-й Красногвардейский пр-д, 21 стр. 2',
                '1-й Красногвардейский пр-д, 21 стр. 3','1-й Красногвардейский пр-д, 21 корп. 2',
                '1-й Красногвардейский проспект, 21 стр. 2','1-й Красногвардейский пр., 21 стр. 2'):
            self.assertNotEqual(expected,tiles.normalized_address(different))

    def test_observed_building_abbreviations_match(self):
        self.assertEqual(tiles.normalized_address('Москва, набережная Академика Туполева, 1/7 с2'),
                         tiles.normalized_address('наб. Академика Туполева, 1/7 стр. 2'))
        self.assertEqual(tiles.normalized_address('Москва, улица Примерная, 10к2'),
                         tiles.normalized_address('Примерная ул., 10 корпус 2'))

    def test_stopped_beskudnikovo_address_matches_dotted_corpus(self):
        expected = tiles.normalized_address('Москва, Бескудниковский бульвар, 11 к1')
        for header in ('Бескудниковский б-р, 11 к. 1',
                       'Бескудниковский бульвар, 11к.1',
                       'Бескудниковский б-р, 11 корп. 1'):
            with self.subTest(header=header):
                self.assertEqual(expected, tiles.normalized_address(header))

    def test_dotted_corpus_preserves_house_corpus_and_building_type(self):
        expected = tiles.normalized_address('Москва, Бескудниковский бульвар, 11 к1')
        for header in ('Бескудниковский б-р, 11 к. 2',
                       'Бескудниковский б-р, 12 к. 1',
                       'Бескудниковский б-р, 11 стр. 1',
                       'Бескудниковский б-р, 11 к. 1а',
                       'Бескудниковский б-р, 11/1 к. 1',
                       'Другой б-р, 11 к. 1'):
            with self.subTest(header=header):
                self.assertNotEqual(expected, tiles.normalized_address(header))

    def test_dotted_corpus_header_confirms_only_with_the_requested_coordinates(self):
        # Synthetic protocol response using the actual reported header and input coordinates.
        zone = dict(h3_index='8711aa70effffff',
                    address='Москва, Бескудниковский бульвар, 11 к1',
                    address_lat='55.862252', address_lon='37.560196')
        response = copy.deepcopy(FIXTURE['root'])
        response['location']['current']['geoCoordinate'] = {
            'latitude': 55.862252, 'longitude': 37.560196}
        tiles.widget(response, 'addressBookBarWeb-')['customCell']['cells'][0]['text']['text'] = (
            'Бескудниковский б-р, 11 к. 1')
        c = Mock(); c.entrypoint.return_value = response
        location = FreshLocation(c); location.current_zone = zone; location.delivery_available = True
        result = location.confirm()
        self.assertEqual(result['verified_address'], 'Бескудниковский б-р, 11 к. 1')
        response['location']['current']['geoCoordinate']['latitude'] = 55.9
        with self.assertRaises(core.ParseError):
            location.confirm()

    def test_building_numbers_and_corpus_are_not_discarded(self):
        self.assertNotEqual(tiles.normalized_address('Примерная, 10 с2'),tiles.normalized_address('Примерная, 10 с3'))
        self.assertNotEqual(tiles.normalized_address('Примерная, 10 к2'),tiles.normalized_address('Примерная, 10 с2'))

    def test_real_five_request_workflow_confirms_house(self):
        c = client()
        context = FreshLocation(c).switch(ZONE)
        self.assertTrue(context["delivery_available"])
        self.assertEqual(context["verified_address"], "Бакунинская ул., 60/2")
        self.assertEqual(context["store_id"], "")
        self.assertEqual(c.entrypoint.call_count, 5)
        self.assertIn("lat=55.777199", c.entrypoint.call_args_list[1].args[0])
        self.assertIn("pid=5", c.entrypoint.call_args_list[2].args[0])
        self.assertIn("pid=7", c.entrypoint.call_args_list[3].args[0])
        self.assertEqual(c.entrypoint.call_args_list[3].args[1]["form"]["receiverPhone"], "")

    def test_no_delivery_is_explicit_and_no_save_is_attempted(self):
        initial, house = copy.deepcopy(FIXTURE["initial"]), copy.deepcopy(FIXTURE["house"])
        tiles.widget(house, "addressEditMap-")["centerPin"]["isAvailable"] = False
        c = Mock(); c.entrypoint.side_effect = [initial, house]
        location = FreshLocation(c)
        self.assertFalse(location.switch(ZONE)["delivery_available"])
        self.assertEqual(c.entrypoint.call_count, 2)
        with self.assertRaises(core.ParseError):
            location.confirm()

    def test_wrong_coordinates_cannot_save_a_house(self):
        house = copy.deepcopy(FIXTURE["house"])
        tiles.widget(house, "addressEditMap-")["centerPin"]["coordinates"]["latitude"] = 55.9
        c = Mock(); c.entrypoint.side_effect = [FIXTURE["initial"], house]
        with self.assertRaises(core.ParseError):
            FreshLocation(c).switch(ZONE)
        self.assertEqual(c.entrypoint.call_count, 2)

    def test_nan_coordinates_are_rejected(self):
        house = copy.deepcopy(FIXTURE["house"])
        tiles.widget(house, "addressEditMap-")["centerPin"]["coordinates"]["latitude"] = float("nan")
        with self.assertRaises(core.ParseError):
            FreshLocation.pin(house, ZONE)

    def test_final_house_header_must_match_not_only_coordinates(self):
        data = [copy.deepcopy(FIXTURE[k]) for k in ("initial", "house", "deliver", "save", "root")]
        tiles.widget(data[-1], "addressBookBarWeb-")["customCell"]["cells"][0]["text"]["text"] = "Новощукинская ул., 2"
        c = Mock(); c.entrypoint.side_effect = data
        with self.assertRaises(core.ParseError):
            FreshLocation(c).switch(ZONE)

    def test_unexpected_address_action_cannot_leave_modal(self):
        data = copy.deepcopy(FIXTURE["house"])
        widget = tiles.widget(data, "addressEditConfirmButton-")
        widget["buttonsWeb"][0]["action"]["link"] = "/my/orders"
        with self.assertRaises(core.ParseError):
            FreshLocation.button_link(data, "Привезти сюда")

    def test_missing_availability_is_not_no_delivery(self):
        data = copy.deepcopy(FIXTURE["house"])
        del tiles.widget(data, "addressEditMap-")["centerPin"]["isAvailable"]
        with self.assertRaises(core.ParseError):
            FreshLocation.pin(data, ZONE)


class ProtectionAndPauseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.path = Path(self.tmp.name)/"transport.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_retry_until_increased_pause_elapsed(self):
        core.write_json(self.path, {"blocked": True, "blocked_at_unix": time.time()})
        with self.assertRaises(core.ProtectionError):
            BrowserTransport.prepare_retry_after_pause(self.path, 3, 120)
        self.assertTrue(core.load_json(self.path)["blocked"])

    def test_explicit_later_check_keeps_increased_interval(self):
        core.write_json(self.path, {"blocked": True, "blocked_at_unix": time.time()-180})
        BrowserTransport.prepare_retry_after_pause(self.path, 3, 120)
        data = core.load_json(self.path)
        self.assertFalse(data["blocked"])
        self.assertEqual(data["min_interval"], 3)
        self.assertEqual(data["explicit_checks_after_pause"], 1)

    def test_repeated_failures_do_not_produce_an_endless_retry_loop(self):
        core.write_json(self.path, {"blocked": True, "blocked_at_unix": time.time()-180, "explicit_checks_after_pause": 2})
        with self.assertRaises(core.ProtectionError):
            BrowserTransport.prepare_retry_after_pause(self.path, 3, 120)

    def test_explicit_manual_recovery_can_resume_same_profile_after_cooldown(self):
        core.write_json(self.path,{'blocked':True,'reason':'HTTP403','blocked_at_unix':time.time()-180,'explicit_checks_after_pause':2})
        BrowserTransport.prepare_retry_after_pause(self.path,3,120,manual_access_confirmed=True)
        state=core.load_json(self.path)
        self.assertFalse(state['blocked']);self.assertEqual(state['explicit_manual_access_confirmations'],1)
        self.assertEqual(state['min_interval'],3)

    def guarded_client(self, response):
        from threading import Event
        from ozon_backoff import BackoffGate
        c = BrowserTransport.__new__(BrowserTransport)
        c.state_path, c.state = self.path, {}
        c.deadline, c.interval, c.base_interval = time.monotonic()+60, 15, 15
        c.max_requests, c.count, c.page = 5, 0, Mock()
        c.page.evaluate.return_value = response
        c.backoff_gate = BackoffGate(self.path.parent, Event(), interval=15,
            max_interval=15, on_403_interval=15, on_429_interval=15,
            sleep=Mock(side_effect=core.BudgetError('Offline test ends at protected wait')))
        return c

    def test_observed_json_challenge_requires_manual_before_retry_and_survives_restart(self):
        from threading import Event
        from ozon_backoff import BackoffGate
        observed = core.load_json(Path(__file__).parent/'testdata/actual_403_challenge_url_20261030.json')
        self.assertFalse(observed['synthetic'])
        c = self.guarded_client(observed['response'])
        with self.assertRaises(core.BudgetError):
            c.entrypoint('/category/supermarket-voda-soki-napitki-9467000/?page=14')
        self.assertGreater(c.backoff_gate.manual_since, 0)
        c.page.evaluate.assert_called_once()
        saved = core.load_json(self.path.parent/'backoff_status.json')
        self.assertTrue(saved['requires_manual_access_check'])
        self.assertEqual(saved['request_interval_seconds'], 15)
        resumed = BackoffGate(self.path.parent, Event(), interval=15,
            max_interval=15, on_403_interval=15, on_429_interval=15)
        self.assertEqual(resumed.manual_since, c.backoff_gate.manual_since)
        self.assertFalse(resumed.signal.exists())

    def test_plain403_remains_nonmanual_with_fixed15second_wait(self):
        c = self.guarded_client({'status':403, 'type':'application/json',
            'text':'{"message":"Access denied"}'})
        with self.assertRaises(core.BudgetError):
            c.entrypoint('/category/supermarket-25000/?miniapp=supermarket')
        self.assertEqual(c.backoff_gate.manual_since, 0)
        self.assertEqual(c.backoff_gate.interval, 15)
        self.assertAlmostEqual(c.backoff_gate.until-c.backoff_gate.last_failure_unix, 15)
        c.page.evaluate.assert_called_once()

    def test_json_challenge_on_http200_cannot_pass_as_storefront_data(self):
        observed = core.load_json(Path(__file__).parent/'testdata/actual_403_challenge_url_20261030.json')
        response = dict(observed['response'], status=200)
        c = self.guarded_client(response)
        with self.assertRaisesRegex(core.ProtectionError, '(?i)challenge'):
            c._request_once({'url':'https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url=%2Fcategory%2Fsupermarket-25000%2F'})
        self.assertTrue(c.state['blocked'])
        c.page.evaluate.assert_called_once()

    def test_403_stops_fetch_before_another_request(self):
        c = BrowserTransport.__new__(BrowserTransport)
        c.state_path, c.state = self.path, {}
        c.deadline, c.interval, c.max_requests, c.count = time.monotonic()+60, 3, 5, 0
        c.page = Mock()
        c.page.evaluate.return_value = {"status": 403, "type": "text/html", "text": "Access denied"}
        for _ in range(2):
            with self.assertRaises(core.ProtectionError):
                c.entrypoint("/category/supermarket-25000/?miniapp=supermarket")
        self.assertEqual(c.page.evaluate.call_count, 1)
        self.assertTrue(core.load_json(self.path)["blocked"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
