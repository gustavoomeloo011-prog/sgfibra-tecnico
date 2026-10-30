import unittest
from datetime import date
from unittest.mock import patch
from pathlib import Path
from app.sgp import SGPClient, Credentials


class ItineraryTests(unittest.TestCase):
    def test_chronology_grouping_and_missing_coordinates(self):
        client = SGPClient('https://example.test', Credentials('test','unused'))
        def order(id, status, scheduled=None, finished=None, technician='Paulo'):
            return {'os_id':id,'os_status':status,'os_tecnico_responsavel':technician,
                    'os_data_agendamento':scheduled,'os_data_finalizacao':finished,'cliente':'Teste'}
        orders = [order(1,0,'2026-09-25 14:00:00'),order(2,0,'2026-09-25 10:00:00'),
                  order(3,1,finished='2026-09-25 09:00:00'),order(4,1,finished='2026-09-25 08:00:00'),
                  order(5,2,'2026-09-25 09:30:00'),order(6,3),
                  order(7,0,'2026-09-25 07:00:00',technician='Kaique')]
        with patch.object(client,'route_monitor_orders',return_value=orders), \
             patch('app.sgp._cached_order_coordinates',side_effect=lambda c,id: (None,None) if id==6 else (-23.5,-46.6)) as coords:
            result = client.technician_itinerary('Paulo',date(2026,9,25))
        self.assertEqual([s['id'] for s in result['completed']],[4,3])
        self.assertEqual([s['id'] for s in result['current']],[5])
        self.assertEqual([s['id'] for s in result['upcoming']],[2,1,6])
        self.assertEqual(result['missing_coordinates'],1)
        self.assertEqual(coords.call_count,6)

    def test_empty_day(self):
        client = SGPClient('https://example.test',Credentials('test','unused'))
        with patch.object(client,'route_monitor_orders',return_value=[]):
            result = client.technician_itinerary('Paulo',date(2026,9,25))
        self.assertEqual(result['upcoming'],[])
        self.assertEqual(result['completed'],[])

    def test_map_itinerary_uses_plain_sequence_and_reduced_context_opacity(self):
        source = Path("static/essential.js").read_text(encoding="utf-8")
        itinerary = source[source.index("function drawTechnicianItinerary"):source.index("function serviceMapDistance")]
        self.assertIn("const orderNumber = item => allStops.indexOf(item) + 1", itinerary)
        self.assertIn("drawItineraryRoads", itinerary)
        self.assertIn("No straight-line fallback", itinerary)
        self.assertIn("itinerary-direction", itinerary)
        self.assertNotIn("${prefix}${index+1}", itinerary)
        map_renderer = source[source.index("function renderCombinedServiceMap"):source.index("async function loadCombinedServiceMap")]
        self.assertIn("? .3 : 1", map_renderer)
        self.assertIn("? .5 : 1", map_renderer)
