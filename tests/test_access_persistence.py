import hashlib
import time
import unittest
from unittest.mock import patch
from app_fastapi import postgres_operations as pg
from app_fastapi.state import StateStore


class DatabaseStartupTests(unittest.TestCase):
    def test_configured_database_never_silently_falls_back(self):
        with patch.dict('os.environ', {'DATABASE_URL': 'postgresql://test.invalid/db'}), \
             patch.object(pg, 'ConnectionPool', side_effect=RuntimeError('offline')) as pool, \
             patch.object(pg.time, 'sleep'), patch.object(pg, '_pool', None), patch.object(pg, 'POSTGRES_ACTIVE', False):
            with self.assertRaisesRegex(RuntimeError, 'Fallback local bloqueado'):
                pg.configure_operations()
            self.assertEqual(pool.call_count, 3)

    def test_missing_production_database_does_not_start_local_database(self):
        with patch.dict('os.environ', {'DATABASE_URL': '', 'EBF_DATABASE_URL': '', 'RENDER': 'true'}):
            with self.assertRaisesRegex(RuntimeError, 'persistente não configurado'):
                pg.configure_operations()


class SessionPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rows = {}
        def save(key, payload, expiry):
            if key not in self.rows or self.rows[key]['payload']:
                self.rows[key] = {'payload': payload, 'expires_at': expiry}
        def revoke(key):
            self.rows[key] = {'payload': b'', 'expires_at': time.time() + 1000}
        for target, value in [('POSTGRES_ACTIVE', True), ('session_record', lambda key: self.rows.get(key)),
                              ('save_session_record', save), ('revoke_session_record', revoke),
                              ('active_session_records', lambda: [r for r in self.rows.values() if r['payload']])]:
            change = patch.object(pg, target, value)
            change.start()
            self.addCleanup(change.stop)

    async def test_session_survives_switching_worker_without_redis(self):
        first, second = StateStore(), StateStore()
        sid = await first.create_session({'user': {'usuario': 'teste'}, 'credential_password': 'segredo'}, 60)
        data = await second.get_session(sid)
        self.assertEqual(data['user']['usuario'], 'teste')
        row = self.rows[hashlib.sha256(sid.encode()).hexdigest()]
        self.assertNotIn(b'segredo', row['payload'])
        self.assertFalse(first.memory)

    async def test_revocation_wins_over_stale_cache_and_concurrent_save(self):
        first, second = StateStore(), StateStore()
        sid = await first.create_session({'user': {'usuario': 'teste'}}, 60)
        data = await second.get_session(sid)
        await second.set('session:' + sid, data, 60)
        await first.delete_session(sid)
        await second.save_session(sid, data)
        self.assertIsNone(await second.get_session(sid))

    async def test_legacy_session_migrates_without_extending_expiration(self):
        store = StateStore()
        expiry = time.time() + 30
        await store.set('session:legacy', {'user': {'usuario': 'teste'}, 'expires': expiry, 'ttl': 300}, 30)
        self.assertEqual((await store.get_session('legacy'))['expires'], expiry)
        self.assertEqual(self.rows[hashlib.sha256(b'legacy').hexdigest()]['expires_at'], expiry)

    async def test_expired_session_is_not_recovered_from_cache(self):
        store = StateStore()
        await store.save_session('expired', {'expires': time.time() - 1})
        await store.set('session:expired', {'expires': time.time() + 100}, 100)
        self.assertIsNone(await store.get_session('expired'))

    async def test_database_error_is_not_interpreted_as_missing_session(self):
        with patch.object(pg, 'session_record', side_effect=pg.psycopg.OperationalError('offline')):
            with self.assertRaises(pg.psycopg.OperationalError):
                await StateStore().get_session('existing')
