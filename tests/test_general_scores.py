import asyncio
import unittest
from unittest.mock import Mock, patch

from app.sgp import SGPError
from starlette.requests import Request

from app_fastapi import main


class GeneralScoreTests(unittest.TestCase):
    def test_general_uses_one_team_calculation_and_includes_zero_score_technicians(self):
        request = Request({"type": "http", "query_string": b"general=1&month=2026-09"})
        context = ("session", {"user": {"mode": "superadmin"}}, Mock())

        async def cached(_session, _key, _ttl, loader, **kwargs):
            return loader()

        with patch.object(main, "cached", cached), \
             patch.object(main.services, "_superadmin_scores", return_value={
                 "technicians": [{"name": "Arthur Souza", "points": 10}],
             }) as calculate, \
             patch.object(main, "platform_users", return_value=[
                 {"name": "Arthur Souza", "username": "Arthur", "role": "technician"},
                 {"name": "Bruno", "username": "Bruno", "role": "technician"},
                 {"name": "Alex", "username": "Alex", "role": "disabled"},
             ]):
            result = asyncio.run(main.scores(request, context))
        calculate.assert_called_once_with(context[2], context[1], "", "2026-09")
        self.assertEqual([(row["name"], row["points"]) for row in result["technicians"]],
                         [("Arthur Souza", 10), ("Bruno", 0)])

    def test_technician_cannot_request_team_scores(self):
        request = Request({"type": "http", "query_string": b"general=1"})
        with self.assertRaises(SGPError) as caught:
            asyncio.run(main.scores(request, ("session", {"user": {"mode": "technician"}}, Mock())))
        self.assertEqual(caught.exception.status, 403)
