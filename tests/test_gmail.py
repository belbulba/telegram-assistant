import base64
from email import policy
from email.parser import BytesParser
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from assistant.agent import Agent
from assistant.calendar import Calendar
from assistant.gmail import Gmail, note_message
from assistant.store import Store


class GmailTests(unittest.IsolatedAsyncioTestCase):
    def test_note_format_preserves_text_and_escapes_html(self):
        raw = note_message('Идея', 'Строка 1\n<script>данные</script>', 'test@example.invalid')
        message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
        self.assertEqual(message['Subject'], 'Идея')
        self.assertEqual(message['X-Uniform-Type-Identifier'], 'com.apple.mail-note')
        self.assertIn('Строка 1<br>&lt;script&gt;данные&lt;/script&gt;', message.get_content())

    async def test_invalid_title_does_not_access_gmail(self):
        gmail = Gmail(None)
        gmail.request = AsyncMock()
        with self.assertRaises(ValueError):
            await gmail.create_note('name\nInjected: value', 'Text')
        gmail.request.assert_not_called()

    async def test_create_uses_notes_label_without_inbox_or_sending(self):
        gmail = Gmail(None)
        gmail.request = AsyncMock(side_effect=[{'emailAddress': 'test@example.invalid'},
            {'labels': [{'id': 'notes1', 'name': 'Notes'}]}, {'id': 'message1'}])
        result = await gmail.create_note('Тест', 'Содержание')
        self.assertEqual(result['status'], 'created')
        self.assertEqual(gmail.request.call_args.args, ('POST', 'messages'))
        self.assertEqual(gmail.request.call_args.kwargs['json']['labelIds'], ['notes1'])

    async def test_agent_does_not_repeat_same_uncertain_write(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'db')
            self.addCleanup(store.db.close)
            def call(number):
                item = SimpleNamespace(type='function_call', name='create_note',
                    arguments='{"title":"Тест","content":"Текст"}', call_id='call'+str(number))
                item.model_dump = lambda **kw: {'type': 'function_call', 'name': item.name,
                    'arguments': item.arguments, 'call_id': item.call_id, 'id': 'fc'+str(number)}
                return item
            create = AsyncMock(side_effect=[SimpleNamespace(output=[call(1)]), SimpleNamespace(output=[call(2)]),
                SimpleNamespace(output=[], output_text='Проверьте Notes.')])
            gmail = SimpleNamespace(create_note=AsyncMock(side_effect=RuntimeError('Нет ответа Gmail')))
            agent = Agent(SimpleNamespace(responses=SimpleNamespace(create=create)), 'test', Calendar(None), store, gmail=gmail)
            text, buttons = await agent.answer('Создай заметку')
            self.assertEqual(text, 'Проверьте Notes.')
            gmail.create_note.assert_awaited_once()
            self.assertTrue(any(t.get('name') == 'create_note' for t in create.call_args.kwargs['tools']))
            self.assertEqual(buttons, [])
