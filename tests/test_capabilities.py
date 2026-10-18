from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock

from assistant.agent import Agent
from assistant.calendar import Calendar
from assistant.store import Store


class CapabilitiesTests(unittest.IsolatedAsyncioTestCase):
    async def test_reference_survives_reset_and_reflects_actual_gmail_availability(self):
        for connected in (False, True):
            with self.subTest(connected=connected), tempfile.TemporaryDirectory() as folder:
                store = Store(Path(folder) / 'db')
                try:
                    create = AsyncMock(return_value=SimpleNamespace(output=[], output_text='Ответ'))
                    agent = Agent(SimpleNamespace(responses=SimpleNamespace(create=create)), 'test',
                        Calendar(None), store, gmail=object() if connected else None)
                    store.add([{'role': 'assistant', 'content': 'Я могу читать и удалять письма.'}])
                    await agent.answer('Можешь читать почту?')
                    instructions = create.call_args.kwargs['instructions']
                    self.assertIn(agent.capabilities, instructions)
                    self.assertIn('ещё не реализована', instructions)
                    self.assertIn('НЕ подключено' if not connected else 'создание заметок Gmail подключено', instructions)
                    store.clear()
                    await agent.answer('Что умеешь?')
                    self.assertIn(agent.capabilities, create.call_args.kwargs['instructions'])
                    self.assertEqual(any(t.get('name') == 'create_note' for t in create.call_args.kwargs['tools']), connected)
                finally:
                    store.db.close()
