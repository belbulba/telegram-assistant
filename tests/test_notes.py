import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
from assistant.agent import Agent
from assistant.auth import NOTE_SCOPES
from assistant.calendar import Calendar
from assistant.notes import Notes
from assistant.store import Store


class NotesTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_and_validate(self):
        graph = SimpleNamespace(request=AsyncMock(return_value={'id': 'note1'}))
        notes = Notes(graph)
        result = await notes.create(' Идеи ', 'Текст <буквально>\nВторая строка')
        self.assertEqual(result['status'], 'created')
        graph.request.assert_awaited_once_with('POST', '/me/notes', scopes=NOTE_SCOPES,
            json={'subject': 'Идеи', 'body': {'contentType': 'text', 'content': 'Текст <буквально>\nВторая строка'}})
        graph.request.reset_mock()
        with self.assertRaises(ValueError):
            await notes.create('Название', ' ')
        graph.request.assert_not_called()

    async def test_uncertain_write_is_not_retried(self):
        graph = SimpleNamespace(request=AsyncMock(side_effect=httpx.ReadTimeout('private details')))
        with self.assertRaisesRegex(RuntimeError, 'могла сохраниться'):
            await Notes(graph).create('Тест', 'Текст')
        self.assertEqual(graph.request.await_count, 1)

    async def test_missing_id_is_not_success(self):
        graph = SimpleNamespace(request=AsyncMock(return_value={}))
        with self.assertRaisesRegex(RuntimeError, 'не подтвердил'):
            await Notes(graph).create('Тест', 'Текст')

    async def test_agent_routes_note_without_calendar_write(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'db')
            call = SimpleNamespace(type='function_call', name='create_note',
                arguments='{"title":"Идея","content":"Купить молоко"}', call_id='c1')
            call.model_dump = lambda **kw: {'type': 'function_call', 'name': call.name,
                'arguments': call.arguments, 'call_id': call.call_id, 'id': 'fc1'}
            create = AsyncMock(side_effect=[SimpleNamespace(output=[call]),
                SimpleNamespace(output=[], output_text='Заметка сохранена.')])
            calendar = Calendar(None)
            calendar.request = AsyncMock(return_value={'id': 'note1'})
            calendar.write = AsyncMock()
            agent = Agent(SimpleNamespace(responses=SimpleNamespace(create=create)), 'test', calendar, store)
            text, buttons = await agent.answer('Сохрани заметку: купить молоко')
            self.assertEqual(text, 'Заметка сохранена.')
            self.assertEqual(buttons, [])
            calendar.write.assert_not_called()
            self.assertEqual(calendar.request.call_args.args, ('POST', '/me/notes'))
            store.db.close()
