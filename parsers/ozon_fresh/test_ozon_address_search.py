"""Actual disabled-house response and strict address-search continuation."""
import copy
from pathlib import Path
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import ozon_address_search as search
import ozon_fresh_fast as core
import ozon_har_catalog as tiles
from ozon_live_parser import FreshLocation

FIXTURE = core.load_json(Path(__file__).parent / 'testdata/actual_house_search_20261028.json')
ZONE = FIXTURE['zone']


def client():
    result = Mock()
    result.entrypoint.side_effect = [copy.deepcopy(FIXTURE[k]) for k in ('initial', 'house', 'selected', 'deliver', 'save', 'root')]
    result.request.side_effect = [copy.deepcopy(FIXTURE[k]) for k in ('suggestions', 'validation')]
    return result


def changed_link(response, **changes):
    link = response['data']['action']['link']
    u = urlsplit(link)
    pairs = dict(parse_qsl(u.query))
    pairs.update(changes)
    response['data']['action']['link'] = urlunsplit((u.scheme, u.netloc, u.path, urlencode(pairs), u.fragment))


class HouseSearchTests(unittest.TestCase):
    def test_enabled_neighbour_house_uses_exact_fresh_search_before_save(self):
        house = copy.deepcopy(FIXTURE['house'])
        form, field = search.address_field(house)
        field['value'] = 'Москва, Волоколамское шоссе, 13а'
        button = tiles.widget(house, 'addressEditConfirmButton-')['buttonsWeb'][0]
        button.pop('disabled', None); button['theme'] = 'fresh'
        c = client()
        c.entrypoint.side_effect = [copy.deepcopy(FIXTURE['initial']), house] + [
            copy.deepcopy(FIXTURE[k]) for k in ('selected', 'deliver', 'save', 'root')]
        result = FreshLocation(c).switch(ZONE)
        self.assertEqual(result['verified_address'], 'Волоколамское ш., 13')
        self.assertEqual(c.request.call_count, 2)

    def test_neighbour_recovery_requires_fresh_search_field_and_retains_house_suffix(self):
        house = copy.deepcopy(FIXTURE['house'])
        form, field = search.address_field(house)
        field['value'] = 'Москва, Волоколамское шоссе, 13а'
        button = tiles.widget(house, 'addressEditConfirmButton-')['buttonsWeb'][0]
        button.pop('disabled', None); button['theme'] = 'fresh'
        self.assertTrue(search.house_search_required(house, ZONE))
        field['value'] = ZONE['address']
        self.assertFalse(search.house_search_required(house, ZONE))
        field['value'] = 'Москва, Волоколамское шоссе, 13а'
        form['isFresh'] = False
        self.assertFalse(search.house_search_required(house, ZONE))
        self.assertNotEqual(tiles.normalized_address(ZONE['address']), tiles.normalized_address(field['value']))

    def test_actual_disabled_house_replays_successfully_through_public_search(self):
        self.assertFalse(FIXTURE['synthetic'])
        c = client()
        result = FreshLocation(c).switch(ZONE)
        self.assertEqual(result['verified_address'], 'Волоколамское ш., 13')
        self.assertTrue(result['delivery_available'])
        self.assertEqual(result['store_id'], '')
        self.assertEqual(c.entrypoint.call_count, 6)
        self.assertEqual(c.request.call_count, 2)
        spec = c.request.call_args_list[1].args[0]
        self.assertEqual(spec['json']['index'], 0)
        self.assertEqual(spec['json']['suggestValue'], FIXTURE['suggestions']['suggestions'][0]['address']['fullText'])

    def test_disabled_button_is_not_an_external_or_empty_link(self):
        with self.assertRaisesRegex(core.ParseError, 'disabled'):
            FreshLocation.button_link(FIXTURE['house'], 'Привезти сюда')

    def test_same_house_provider_duplicates_keep_original_rank(self):
        index, text = search.select_suggestion(FIXTURE['suggestions'], ZONE)
        self.assertEqual(index, 0)
        self.assertEqual(text, 'город Москва, Волоколамское шоссе, 13')

    def test_nearby_house_13_slash_1_is_not_house_13(self):
        response = copy.deepcopy(FIXTURE['suggestions'])
        response['suggestions'] = [response['suggestions'][3]]
        with self.assertRaises(core.ParseError):
            search.select_suggestion(response, ZONE)

    def test_wrong_query_cannot_supply_a_house(self):
        response = copy.deepcopy(FIXTURE['suggestions']); response['sourceQuery'] = 'другой адрес'
        with self.assertRaises(core.ParseError):
            search.select_suggestion(response, ZONE)

    def test_wrong_country_city_house_and_untrusted_geometry_are_rejected(self):
        for key in ('country', 'city', 'house', 'trust'):
            response = copy.deepcopy(FIXTURE['suggestions']); entry = response['suggestions'][0]
            if key == 'country':entry['address']['isoAlpha2CountryCode'] = 'AZ'
            elif key == 'city':entry['address']['components']['city']['name'] = 'Другой город'
            elif key == 'house':entry['address']['components']['house']['number'] = '13/1'
            else:entry['trustCoord'] = False
            with self.subTest(key=key), self.assertRaises(core.ParseError):
                search.select_suggestion(response, ZONE)

    def test_one_conflicting_duplicate_is_not_silently_ignored(self):
        response = copy.deepcopy(FIXTURE['suggestions'])
        response['suggestions'][2]['address']['geometry']['point']['lat'] = 55.9
        with self.assertRaises(core.ParseError):
            search.select_suggestion(response, ZONE)

    def test_nan_search_coordinates_are_rejected(self):
        response = copy.deepcopy(FIXTURE['suggestions'])
        response['suggestions'][0]['address']['geometry']['point']['lat'] = float('nan')
        with self.assertRaises(core.ParseError):
            search.select_suggestion(response, ZONE)

    def test_search_requires_the_published_fresh_validation_action(self):
        for change in ('scope', 'path', 'field_action'):
            data = copy.deepcopy(FIXTURE['house']); form, field = search.address_field(data)
            if change == 'scope':form['isFresh'] = False
            elif change == 'path':field['searchOptions']['link'] = 'https://example.com/validateCourierAddressV2'
            else:field['searchOptions']['action']['link'] = 'otherAction'
            with self.subTest(change=change), self.assertRaises(core.ParseError):
                search.validation_request(data, ZONE, 0, 'Москва, Волоколамское шоссе, 13')

    def test_validation_requires_exact_available_house(self):
        for key in ('isExact', 'isAvailable'):
            response = copy.deepcopy(FIXTURE['validation']);response['data'][key] = False
            with self.subTest(key=key), self.assertRaises(core.ParseError):
                search.validated_link(response, ZONE, 'город Москва, Волоколамское шоссе, 13')

    def test_validated_action_cannot_leave_ozon_fresh_modal(self):
        for link in ('/my/orders', 'https://example.com/modal/fresh'):
            response = copy.deepcopy(FIXTURE['validation']);response['data']['action']['link'] = link
            with self.subTest(link=link), self.assertRaises(core.ParseError):
                search.validated_link(response, ZONE, 'город Москва, Волоколамское шоссе, 13')

    def test_validation_cannot_change_name_scope_or_coordinates(self):
        for key, value in [('suggestValue', 'город Москва, Волоколамское шоссе, 13/1'),
                           ('searchString', 'Москва, Волоколамское шоссе, 14'),
                           ('miniapp', 'other'), ('lat', '55.9')]:
            response = copy.deepcopy(FIXTURE['validation']);changed_link(response, **{key:value})
            with self.subTest(key=key), self.assertRaises(core.ParseError):
                search.validated_link(response, ZONE, 'город Москва, Волоколамское шоссе, 13')

    def test_selected_house_name_is_checked_before_saving(self):
        values = [copy.deepcopy(FIXTURE[k]) for k in ('initial', 'house', 'selected')]
        _, field = search.address_field(values[-1]);field['value'] = 'Москва, Волоколамское шоссе, 14'
        c = client();c.entrypoint.side_effect = values
        with self.assertRaises(core.ParseError):FreshLocation(c).switch(ZONE)
        self.assertEqual(c.entrypoint.call_count, 3)

    def test_selected_coordinates_are_checked_before_saving(self):
        values = [copy.deepcopy(FIXTURE[k]) for k in ('initial', 'house', 'selected')]
        tiles.widget(values[-1], 'addressEditMap-')['centerPin']['coordinates']['latitude'] = 55.9
        c = client();c.entrypoint.side_effect = values
        with self.assertRaises(core.ParseError):FreshLocation(c).switch(ZONE)
        self.assertEqual(c.entrypoint.call_count, 3)

    def test_final_house_name_and_coordinates_remain_required(self):
        for change in ('name', 'coordinates'):
            values = [copy.deepcopy(FIXTURE[k]) for k in ('initial', 'house', 'selected', 'deliver', 'save', 'root')]
            if change == 'name':tiles.widget(values[-1], 'addressBookBarWeb-')['customCell']['cells'][0]['text']['text'] = 'Волоколамское ш., 14'
            else:values[-1]['location']['current']['geoCoordinate']['latitude'] = 55.9
            c = client();c.entrypoint.side_effect = values
            with self.subTest(change=change), self.assertRaises(core.ParseError):FreshLocation(c).switch(ZONE)

    def test_other_disabled_button_cannot_trigger_search(self):
        data = copy.deepcopy(FIXTURE['house'])
        form, _ = search.address_field(data);form['isFresh'] = False
        self.assertFalse(search.house_search_required(data))


if __name__ == '__main__':
    unittest.main(verbosity=2)
