import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from types import SimpleNamespace
from assistant.store import Store
from assistant.calendar import Calendar
from assistant.main import allowed
from assistant.agent import Agent

class StoreTests(unittest.TestCase):
    def test_history_and_confirmation_once(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'db')
            store.add([{'role': 'user', 'content': 'привет'}])
            self.assertEqual(store.history()[0]['content'], 'привет')
            key = store.stage({'action': 'delete'})
            self.assertEqual(store.take(key), {'action': 'delete'})
            self.assertIsNone(store.take(key))
            key = store.stage({})
            store.db.execute('UPDATE pending SET created=0')
            store.db.commit()
            self.assertIsNone(store.take(key))
            store.clear()
            self.assertEqual(store.history(), [])
            store.db.close()

class CalendarTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cal = Calendar(None)
        self.args = dict(title='Встреча', start='2026-10-18T15:00:00+03:00', end='2026-10-18T16:00:00+03:00', reminder_minutes=15)

    def test_timezone_and_validation(self):
        self.assertEqual(self.cal.payload(self.args)['start']['dateTime'], '2026-10-18T12:00:00')
        with self.assertRaises(ValueError):
            self.cal.payload({**self.args, 'end': self.args['start']})
        with self.assertRaises(ValueError):
            self.cal.instant('2026-10-18T15:00:00')

    async def test_changed_event_is_not_deleted(self):
        self.cal.get = AsyncMock(return_value={'@odata.etag': 'new'})
        self.cal.request = AsyncMock()
        with self.assertRaises(ValueError):
            await self.cal.write('delete_event', {'event_id': '123'}, 'old')
        self.cal.request.assert_not_called()

    async def test_create_calls_graph(self):
        self.cal.request = AsyncMock(return_value={'id': 'real'})
        await self.cal.write('create_event', {**self.args, 'transaction_id': 'unique'})
        self.assertEqual(self.cal.request.call_args.args, ('POST', '/me/events'))
        self.assertEqual(self.cal.request.call_args.kwargs['json']['transactionId'], 'unique')

class AccessTests(unittest.TestCase):
    def test_owner_private_only(self):
        def update(uid, chat):
            return SimpleNamespace(effective_user=SimpleNamespace(id=uid), effective_chat=SimpleNamespace(type=chat))
        self.assertTrue(allowed(update(42, 'private'), 42))
        self.assertFalse(allowed(update(43, 'private'), 42))
        self.assertFalse(allowed(update(42, 'group'), 42))

class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_only_stages_and_returns_tool_output(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'db')
            call = SimpleNamespace(type='function_call', name='delete_event', arguments='{"event_id":"abc"}', call_id='call1')
            call.model_dump = lambda **kwargs: {'type': 'function_call', 'name': call.name, 'arguments': call.arguments, 'call_id': call.call_id, 'id': 'fc1'}
            responses = AsyncMock(side_effect=[SimpleNamespace(output=[call]), SimpleNamespace(output=[], output_text='Подтвердите удаление.')])
            client = SimpleNamespace(responses=SimpleNamespace(create=responses))
            cal = Calendar(None)
            cal.get = AsyncMock(return_value={'subject': 'Встреча', '@odata.etag': 'v1', 'start': {'dateTime': '2026-10-18T12:00:00'}})
            cal.write = AsyncMock()
            agent = Agent(client, 'test-model', cal, store)
            text, buttons = await agent.answer('Удали встречу')
            cal.write.assert_not_called()
            self.assertEqual(len(buttons), 1)
            pending = store.take(buttons[0][0])
            self.assertEqual(pending['etag'], 'v1')
            self.assertTrue(any(x.get('type') == 'function_call_output' for x in store.history()))
            store.db.close()

if __name__ == '__main__':
    unittest.main()
