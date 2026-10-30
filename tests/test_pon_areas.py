import copy
import unittest
from shapely.geometry import Point, shape
from app.pon_areas import build_geometry, bulk_addresses, street_tile, suggested_manual_areas
from app.sgp import SGPClient, Credentials, SGPError
from unittest.mock import Mock, patch


class PonInventoryTests(unittest.TestCase):
    @patch('app.pon_areas.time.sleep')
    @patch('app.pon_areas.urlopen', side_effect=OSError('network unreachable'))
    @patch('app.pon_areas.operations.save_cache_snapshot')
    @patch('app.pon_areas.operations.cache_snapshot', return_value=None)
    def test_street_download_failure_never_overwrites_saved_data(self, cache, save, fetch, sleep):
        with self.assertRaisesRegex(ValueError, 'fonte de ruas'):
            street_tile((-589, -1162))
        self.assertEqual(fetch.call_count, 3)
        save.assert_not_called()

    @patch('app.pon_areas.operations.save_cache_snapshot')
    @patch('app.pon_areas.operations.cache_snapshot', return_value=None)
    def test_address_pages_follow_offset_and_store_only_postal_fields(self, cache, save):
        client = Mock()
        def response(method, path, form):
            return {'paginacao': {'total': 201}, 'clientes': [{'password': 'never-store', 'contratos': [
                {'id': form['offset'] + 1, 'endereco': {'logradouro': 'Rua Teste', 'numero': '10', 'cep': '08000000'}}]}]}
        client.request.side_effect = response
        result = bulk_addresses(client, Mock())
        self.assertEqual(set(result), {'1', '101', '201'})
        self.assertEqual({call.kwargs['form']['offset'] for call in client.request.call_args_list}, {0, 100, 200})
        self.assertNotIn('never-store', str(save.call_args_list))

    def test_inventory_does_not_request_broken_address_serialization(self):
        client = SGPClient('https://example.invalid', Credentials('test', 'test'))
        client.request = Mock(return_value=[{'id': 1, 'slot': 4, 'pon': 0}])
        self.assertEqual(len(client._olt_onu_inventory('8', 4, 0)), 1)
        query = client.request.call_args.kwargs['query']
        self.assertNotIn('address', query)
        self.assertNotIn('signal', query)
        self.assertEqual(query['pon'], 0)

    @patch('app.sgp.time.sleep')
    def test_transient_invalid_response_is_retried(self, sleep):
        client = SGPClient('https://example.invalid', Credentials('test', 'test'))
        client.request = Mock(side_effect=[SGPError('invalid', 502), [{'id': 1}]])
        self.assertEqual(client._olt_onu_inventory('8', 4, 0), [{'id': 1}])
        self.assertEqual(client.request.call_count, 2)


class PonStreetAreaTests(unittest.TestCase):
    def fixture(self):
        # A street turns at a right angle; another PON's parallel street is
        # nearby. A convex hull would paint the inside of this corner.
        street = {"type": "way", "id": 1, "nodes": [1, 2, 3], "tags": {"highway": "residential", "name": "Rua Teste"},
                  "geometry": [{"lat": -23.5, "lon": -46.6}, {"lat": -23.5, "lon": -46.599}, {"lat": -23.499, "lon": -46.599}]}
        row = {"latitude": -23.50005, "longitude": -46.5995, "olt_id": "3", "slot": 2, "pon": 0,
               "postal_address": {"street": "Rua Teste", "number": "12", "postcode": "08000000"}}
        return row, street

    def test_area_follows_corner_without_filling_its_interior(self):
        row, street = self.fixture()
        areas = build_geometry([row], [street])
        area = shape(areas[("3", 2, 0)]["area_geometry"])
        self.assertTrue(area.contains(Point(-46.5995, -23.5)))
        self.assertTrue(area.contains(Point(-46.599, -23.4995)))
        self.assertFalse(area.contains(Point(-46.5996, -23.4995)))
        self.assertEqual(row["area_status"], "street_matched")

    def test_wrong_street_and_missing_house_number_do_not_generate_area(self):
        row, street = self.fixture()
        row["postal_address"]["street"] = "Rua Outra"
        self.assertEqual(build_geometry([row], [street]), {})
        row["postal_address"]["street"] = "Rua Teste"
        row["postal_address"]["number"] = ""
        self.assertEqual(build_geometry([row], [street]), {})

    def test_house_number_with_conflicting_postcode_is_not_used(self):
        row, street = self.fixture()
        house = {"type": "node", "id": 10, "lat": -23.4991, "lon": -46.599,
                 "tags": {"addr:street": "Rua Teste", "addr:housenumber": "12", "addr:postcode": "99999999"}}
        build_geometry([row], [street, house])
        self.assertEqual(row["location_method"], "sgp_coordinate_street_checked")
        house["tags"]["addr:postcode"] = "08000000"
        build_geometry([row], [street, house])
        self.assertEqual(row["location_method"], "osm_street_house_number")
        self.assertEqual(row["located_latitude"], house["lat"])

    def test_olts_with_identical_slot_and_pon_remain_separate(self):
        row, street = self.fixture()
        other = copy.deepcopy(row)
        other["olt_id"] = "4"
        self.assertEqual(set(build_geometry([row, other], [street])), {("3", 2, 0), ("4", 2, 0)})

    def test_suggested_area_keeps_main_cluster_and_ignores_distant_address(self):
        clients = [
            {"olt_id":"3","slot":1,"pon":1,"latitude":-23.5500,"longitude":-46.6000},
            {"olt_id":"3","slot":1,"pon":1,"latitude":-23.5505,"longitude":-46.6005},
            {"olt_id":"3","slot":1,"pon":1,"latitude":-23.5510,"longitude":-46.6000},
            {"olt_id":"3","slot":1,"pon":1,"latitude":-23.7000,"longitude":-46.8000},
        ]
        result = suggested_manual_areas({"clients":clients,"groups":[{"olt_id":"3","slot":1,"pon":1,"olt":"Itaquera"}]})[0]
        self.assertEqual(result["clients_used"], 3)
        self.assertEqual(result["outliers_ignored"], 1)
        self.assertEqual(result["note"], "Slot 1 · PON 1")
        self.assertFalse(shape(result["geometry"]).contains(Point(-46.8, -23.7)))
