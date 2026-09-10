from __future__ import annotations

import tempfile
import unittest
import time
from pathlib import Path
from unittest.mock import patch

from web_operation_agent.durable import SQLiteSessionStore, DurableStateError
from web_operation_agent.remote import RemoteSessionRegistry, RemoteSessionClosed, RemoteProtocolError
from web_operation_agent.schemas import ApprovalRequest, ApprovalDecision
from web_operation_agent.tests.test_remote_transport import ORIGIN, hello, principal


class DurableSessionTests(unittest.TestCase):
    def test_expired_owner_is_fenced_and_messages_are_never_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteSessionStore(Path(tmp) / 'sessions.db', lease_ms=1000)
            with patch.object(store, 'now', return_value=1000):
                first = store.acquire('s', 'scope')
                self.assertTrue(store.message(first, 'm', 'digest'))
                with self.assertRaises(DurableStateError):
                    store.acquire('s', 'scope')
            with patch.object(store, 'now', return_value=2001):
                with self.assertRaises(DurableStateError):
                    store.acquire('s', 'other-scope')
                second = store.acquire('s', 'scope')
                self.assertGreater(second.fence, first.fence)
                with self.assertRaises(DurableStateError):
                    store.renew(first)
                self.assertFalse(store.message(second, 'm', 'digest'))
                with self.assertRaises(DurableStateError):
                    store.message(second, 'm', 'changed')
                with store.connect() as db:
                    self.assertEqual(db.execute('SELECT state FROM messages').fetchone()[0],
                                     'interrupted')

    def test_registry_recovery_rotates_credentials_and_preserves_duplicate_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteSessionStore(Path(tmp) / 'sessions.db')
            registries = [RemoteSessionRegistry(durable_store=store) for _ in range(2)]
            p = principal(int(time.time() * 1000))
            grant, channel = registries[0].create(allowed_origins=[ORIGIN],
                                                  session_id='recover', principal=p)
            lease = channel.accept_hello(hello('recover', application_id='twenty',
                                  principal_digest=p.binding_digest))['lease_id']
            message = {'message_id': 'm1', 'text': 'read companies', 'page_url': ORIGIN + '/start'}
            channel.accept_message(message, lease_id=lease)
            channel.wait_for_message(timeout_s=0)
            request = ApprovalRequest(1, 'old-approval', 'Create synthetic company',
                                      ('Synthetic',), int(time.time() * 1000) + 30_000,
                                      p.binding_digest, 'm1', 'operation-1')
            channel.request_approval(request)
            decision = ApprovalDecision(1, 'recover', 'old-approval', 'approved',
                                        p.binding_digest, 'm1', 'operation-1')
            channel.accept_approval(decision, lease_id=lease)
            channel.publish('progress', {'state': 'working', 'message': 'Reading'})
            channel.close()
            fresh = principal(int(time.time() * 1000))
            newer, recovered = registries[1].create(allowed_origins=[ORIGIN],
                                                     session_id='recover', principal=fresh)
            try:
                self.assertNotEqual(grant.client_token, newer.client_token)
                new_lease = recovered.accept_hello(hello('recover', application_id='twenty',
                                       principal_digest=fresh.binding_digest))['lease_id']
                self.assertNotEqual(lease, new_lease)
                self.assertTrue(recovered.accept_message(message, lease_id=new_lease)['duplicate'])
                self.assertFalse(recovered._messages)
                with self.assertRaises(RemoteProtocolError):
                    recovered.accept_approval(decision, lease_id=new_lease)
                self.assertFalse(recovered._approvals)
                self.assertFalse(recovered._actions)
                self.assertTrue(store.history(recovered._owner))
            finally:
                recovered.close()


    def test_outbox_failure_closes_channel_before_event_delivery(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteSessionStore(Path(tmp) / 'sessions.db')
            registry = RemoteSessionRegistry(durable_store=store)
            _, channel = registry.create(allowed_origins=[ORIGIN], session_id='disk-failure',
                                         principal=principal(int(time.time() * 1000)))
            count = len(channel._events)
            with patch.object(store, 'append', side_effect=sqlite3.OperationalError('disk full')):
                with self.assertRaises(RemoteSessionClosed):
                    channel.publish('progress', {'state': 'working'})
            self.assertEqual(len(channel._events), count)
            with self.assertRaises(RemoteSessionClosed):
                channel.validate_live()


def _quota_worker(path, output):
    from web_operation_agent.durable import SQLiteProviderQuota
    store = SQLiteSessionStore(path)
    quota = SQLiteProviderQuota(store, scope='tenant-provider', max_requests=2)
    output.put(quota.reserve())


class SharedQuotaTests(unittest.TestCase):
    def test_independent_processes_share_a_quota_and_scope_isolation(self):
        import multiprocessing
        from web_operation_agent.durable import SQLiteProviderQuota
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'store.db'
            store = SQLiteSessionStore(path)
            SQLiteProviderQuota(store, scope='tenant-provider', max_requests=2)
            ctx = multiprocessing.get_context('spawn')
            output = ctx.Queue()
            workers = [ctx.Process(target=_quota_worker, args=(path, output)) for _ in range(4)]
            try:
                for worker in workers:
                    worker.start()
                self.assertEqual(sum(output.get(timeout=10) for _ in workers), 2)
                for worker in workers:
                    worker.join(10)
                    self.assertEqual(worker.exitcode, 0)
            finally:
                for worker in workers:
                    if worker.is_alive():
                        worker.terminate()
                        worker.join()
            self.assertTrue(SQLiteProviderQuota(store, scope='other-tenant', max_requests=2).reserve())
            with self.assertRaises(DurableStateError):
                SQLiteProviderQuota(store, scope='tenant-provider', max_requests=3)

    def test_expired_quota_window_and_inactive_session_retention(self):
        from web_operation_agent.durable import SQLiteProviderQuota
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteSessionStore(Path(tmp) / 'store.db')
            quota = SQLiteProviderQuota(store, scope='test', max_requests=1, window_ms=1000)
            with patch.object(store, 'now', return_value=1000):
                self.assertTrue(quota.reserve())
                self.assertFalse(quota.reserve())
                old = store.acquire('old', 'scope')
                store.message(old, 'm', 'digest')
                store.release(old)
            with patch.object(store, 'now', return_value=2000):
                self.assertTrue(quota.reserve())
            with patch.object(store, 'now', return_value=200_000_000):
                active = store.acquire('active', 'scope')
                self.assertEqual(store.prune(older_than_ms=100_000_000), 1)
                store.renew(active)


def _crash_session(path, ready):
    import os
    store = SQLiteSessionStore(path, lease_ms=1000)
    registry = RemoteSessionRegistry(durable_store=store)
    p = principal(int(time.time() * 1000))
    grant, channel = registry.create(allowed_origins=[ORIGIN], session_id='crashed', principal=p)
    lease = channel.accept_hello(hello('crashed', application_id='twenty',
                                      principal_digest=p.binding_digest))['lease_id']
    channel.accept_message({'message_id': 'crashed-message', 'text': 'create synthetic',
                            'page_url': ORIGIN + '/start'}, lease_id=lease)
    channel.wait_for_message(timeout_s=0)
    ready.send(True)
    ready.close()
    os._exit(0)


class CrashRecoveryTests(unittest.TestCase):
    def test_process_crash_preserves_running_tombstone_without_replay(self):
        import multiprocessing
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'store.db'
            ctx = multiprocessing.get_context('spawn')
            receiver, sender = ctx.Pipe(duplex=False)
            worker = ctx.Process(target=_crash_session, args=(path, sender))
            worker.start()
            try:
                self.assertTrue(receiver.poll(10))
                self.assertTrue(receiver.recv())
                worker.join(10)
                self.assertEqual(worker.exitcode, 0)
                store = SQLiteSessionStore(path, lease_ms=1000)
                with patch.object(store, 'now', return_value=store.now() + 2000):
                    registry = RemoteSessionRegistry(durable_store=store)
                    p = principal(int(time.time() * 1000))
                    _, channel = registry.create(allowed_origins=[ORIGIN],
                                                 session_id='crashed', principal=p)
                    try:
                        self.assertFalse(channel._messages)
                        self.assertFalse(channel._approval_decisions)
                        with store.connect() as db:
                            self.assertEqual(db.execute('SELECT state FROM messages').fetchone()[0],
                                             'interrupted')
                    finally:
                        channel.close()
            finally:
                if worker.is_alive():
                    worker.terminate()
                    worker.join()
                receiver.close()
                sender.close()
