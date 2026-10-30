import asyncio
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from app_fastapi import main
from app_fastapi.state import StateStore
from app.sgp import Credentials, SGPClient, SGPError
from app import operations


class CTOConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for change in (patch.object(operations, 'DB', Path(directory.name) / 'test.db'),
                       patch.object(operations, '_DB_INITIALIZED', False),
                       patch('app_fastapi.postgres_operations.POSTGRES_ACTIVE', False)):
            change.start()
            self.addCleanup(change.stop)

    async def test_second_user_rejected_until_first_finishes_even_after_disconnect(self):
        store = StateStore()
        started, finish = asyncio.Event(), asyncio.Event()
        async def slow_query():
            started.set()
            await finish.wait()
            return 42
        with patch.object(main, 'store', store):
            first = asyncio.create_task(main.exclusive_cto_query(slow_query))
            await started.wait()
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            with self.assertRaises(SGPError):
                await main.exclusive_cto_query(slow_query)
            finish.set()
            await asyncio.gather(*list(main._cto_tasks))
            self.assertEqual(await main.exclusive_cto_query(slow_query), 42)

    async def test_exception_releases_global_slot(self):
        with patch.object(main, 'store', StateStore()):
            async def fail():
                raise ValueError('SGP unavailable')
            with self.assertRaises(ValueError):
                await main.exclusive_cto_query(fail)
            async def ok():
                return 'ok'
            self.assertEqual(await main.exclusive_cto_query(ok), 'ok')

    async def test_old_owner_cannot_release_another_query(self):
        store = StateStore()
        self.assertTrue(await store.acquire_owned('cto', 'new', 120))
        self.assertFalse(await store.maintain_owned('cto', 'old'))
        self.assertFalse(await store.acquire_owned('cto', 'second', 120))
        self.assertTrue(await store.maintain_owned('cto', 'new'))

    async def test_independent_workers_share_database_lock_without_redis(self):
        first, second = StateStore(), StateStore()
        self.assertTrue(await first.acquire_owned('shared', 'first', 120))
        self.assertFalse(await second.acquire_owned('shared', 'second', 120))
        await first.maintain_owned('shared', 'first')
        self.assertTrue(await second.acquire_owned('shared', 'second', 120))


class CTOFastPathTests(unittest.TestCase):
    def test_fast_analysis_keeps_review_warning_without_admin_history_requests(self):
        client = SGPClient('https://example.invalid', Credentials('test', 'test'))
        rows = [{'contract_id': 1, 'client_id': 1, 'belongs': False,
                 'contract_status': 'Ativo', 'slot': 1, 'pon': 2}]
        with patch('app.sgp.build_opener', side_effect=AssertionError('No history on fast path')):
            client._enrich_cto_disconnect_analysis(rows, ('3', '1', '4'), include_history=False)
        self.assertEqual(rows[0]['disconnect_analysis']['decision'], 'review')
        self.assertTrue(rows[0]['disconnect_analysis']['evidence'])
