import unittest
from unittest.mock import patch

from app.server import Handler


class RepeatRefundTests(unittest.TestCase):
    def report(self):
        return {"technicians": [{
            "name": "Bruno Bezerra",
            "repeated": {"details": [{
                "original_order_id": 10, "repeat_order_id": 11,
                "original_suspended": True, "same_technician": True,
                "original_points": 1.0, "repeat_points": 1.0, "refunded": False,
            }]},
        }]}

    def test_historical_refund_recalculates_only_selected_technician(self):
        calls = []

        class Client:
            def team_productivity_report(self, refunds, technician, month):
                calls.append((technician, month))
                return RepeatRefundTests().report()

        handler = object.__new__(Handler)
        session = {"user": {"mode": "superadmin", "role": "superadmin", "usuario": "Gustavo"}}
        body = {
            "month": "2026-07",
            "items": [{
                "technician": "Bruno Bezerra",
                "original_order_id": 10,
                "repeat_order_id": 11,
                "restored_order_ids": [10, 11],
            }],
        }
        with patch("app.server.repeat_refunds", return_value=[]), patch("app.server.refund_repeat") as save:
            result = handler._refund_repeat_score(Client(), session, body)

        self.assertEqual(calls, [("Bruno Bezerra", "2026-07")])
        save.assert_called_once_with(10, 11, "Bruno Bezerra", 2.0, "Gustavo", -1)
        self.assertIn("1 repetida", result["notice"])

    def test_cached_report_avoids_another_sgp_calculation(self):
        class Client:
            def team_productivity_report(self, *_args):
                raise AssertionError("SGP should not be queried again")

        handler = object.__new__(Handler)
        session = {"user": {"mode": "superadmin", "role": "superadmin", "usuario": "Gustavo"}}
        body = {"month": "2026-07", "items": [{
            "technician": "Bruno Bezerra", "original_order_id": 10,
            "repeat_order_id": 11, "restored_order_ids": [10],
        }]}
        with patch("app.server.repeat_refunds", return_value=[]), patch("app.server.refund_repeat") as save:
            handler._refund_repeat_score(Client(), session, body, self.report())
        save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
