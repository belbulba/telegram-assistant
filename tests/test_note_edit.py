import base64
from copy import deepcopy
from email.parser import BytesParser
from email import policy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from assistant.agent import Agent
from assistant.calendar import Calendar
from assistant.gmail import Gmail, note_message
from assistant.main import apply_confirmation
from assistant.store import Store

IDENTITY = 'A84F80A1-9EC7-4020-A502-8C5BCE8BDF50'


def note(message_id='old', content='Дюна', title='Фильмы'):
    raw = note_message(title, content, 'test@example.invalid', note_uuid=IDENTITY)
    parsed = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
    return {'id': message_id, 'labelIds': ['notes1'], 'payload': {'mimeType': 'text/html',
        'headers': [{'name': k, 'value': str(v)} for k, v in parsed.items()],
        'body': {'data': base64.urlsafe_b64encode(parsed.get_payload(decode=True)).decode()}}}


class NoteEditTests(unittest.IsolatedAsyncioTestCase):
    def setup_gmail(self, old=None):
        old = old or note()
        gmail = Gmail(None)
        gmail.notes_label = AsyncMock(return_value='notes1')
        gmail.get_message = AsyncMock(side_effect=[old, old, note('new', 'Дюна\nПрибытие'), old])
        gmail.request = AsyncMock(side_effect=[{'emailAddress': 'test@example.invalid'}, {'id': 'new'}, {'labelIds': ['TRASH']}])
        return gmail, old

    async def test_edit_preserves_identity_and_retires_only_after_verification(self):
        gmail, old = self.setup_gmail()
        result = await gmail.update_note('old', 'Фильмы', 'Дюна\nПрибытие', gmail.note_version(old))
        self.assertEqual(result['status'], 'updated')
        self.assertEqual(gmail.request.call_args.args, ('POST', 'messages/old/trash'))
        insert = gmail.request.call_args_list[1]
        raw = insert.kwargs['json']['raw']
        parsed = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw))
        self.assertEqual(parsed['X-Universally-Unique-Identifier'], IDENTITY)
        self.assertEqual(insert.kwargs['json']['labelIds'], ['notes1'])

    async def test_changed_note_prevents_any_write(self):
        gmail = Gmail(None)
        gmail.get_message = AsyncMock(return_value=note())
        gmail.notes_label = AsyncMock(return_value='notes1')
        gmail.request = AsyncMock()
        with self.assertRaisesRegex(ValueError, 'изменилась'):
            await gmail.update_note('old', 'Фильмы', 'Новый текст', 'stale-version')
        gmail.request.assert_not_called()

    async def test_trashed_or_non_note_and_attachments_are_rejected(self):
        for change in ('trash', 'email', 'attachment'):
            with self.subTest(change=change):
                original = note()
                if change == 'trash':
                    original['labelIds'].append('TRASH')
                elif change == 'email':
                    original['labelIds'] = ['INBOX']
                else:
                    original['payload']['parts'] = [{'filename': 'picture.png'}]
                gmail = Gmail(None)
                gmail.get_message = AsyncMock(return_value=original)
                gmail.notes_label = AsyncMock(return_value='notes1')
                gmail.request = AsyncMock()
                with self.assertRaises(ValueError):
                    await gmail.update_note('old', 'Фильмы', 'Новый текст', gmail.note_version(original))
                gmail.request.assert_not_called()

    async def test_failed_insert_never_trashes_original_or_retries(self):
        gmail, old = self.setup_gmail()
        gmail.request.side_effect = [{'emailAddress': 'test@example.invalid'}, RuntimeError('Uncertain network write')]
        with self.assertRaises(RuntimeError):
            await gmail.update_note('old', 'Фильмы', 'Дюна\nПрибытие', gmail.note_version(old))
        self.assertEqual(gmail.request.await_count, 2)

    async def test_wrong_replacement_or_changed_original_returns_partial(self):
        for mode in ('replacement', 'original'):
            with self.subTest(mode=mode):
                gmail, old = self.setup_gmail()
                replacement = note('new', 'Другой текст' if mode == 'replacement' else 'Дюна\nПрибытие')
                changed = deepcopy(old)
                changed['labelIds'].append('TRASH')
                gmail.get_message.side_effect = [old, old, replacement, changed]
                result = await gmail.update_note('old', 'Фильмы', 'Дюна\nПрибытие', gmail.note_version(old))
                self.assertEqual(result['status'], 'partial')
                self.assertEqual(gmail.request.await_count, 2)

    async def test_cleanup_failure_reports_saved_version_without_retry(self):
        gmail, old = self.setup_gmail()
        gmail.request.side_effect = [{'emailAddress': 'test@example.invalid'}, {'id': 'new'}, RuntimeError('cleanup failed')]
        result = await gmail.update_note('old', 'Фильмы', 'Дюна\nПрибытие', gmail.note_version(old))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['id'], 'new')
        self.assertEqual(gmail.request.await_count, 3)

    async def test_confirmation_routes_update_and_does_not_claim_partial_success(self):
        gmail = SimpleNamespace(update_note=AsyncMock(return_value={'status': 'partial', 'warning': 'Проверьте обе версии'}))
        text = await apply_confirmation({'action': 'update_note', 'args': {'message_id': 'old', 'title': 'Фильмы', 'content': 'Текст', 'expected_version': 'v'}}, None, gmail)
        self.assertEqual(text, 'Проверьте обе версии')

    async def test_agent_requires_fresh_read_and_only_stages_update(self):
        def call(name, args, index):
            item = SimpleNamespace(type='function_call', name=name, arguments=args, call_id='c'+str(index))
            item.model_dump = lambda **kw: {'type': 'function_call', 'name': name, 'arguments': args, 'call_id': item.call_id, 'id': 'fc'+str(index)}
            return item
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'db')
            self.addCleanup(store.db.close)
            args = '{"message_id":"old","title":"Фильмы","content":"Дюна\\nПрибытие"}'
            create = AsyncMock(side_effect=[SimpleNamespace(output=[call('update_note', args, 1)]),
                SimpleNamespace(output=[call('read_note', '{"message_id":"old"}', 2)]),
                SimpleNamespace(output=[call('update_note', args, 3)]), SimpleNamespace(output=[], output_text='Подтвердите изменение.')])
            gmail = SimpleNamespace(read=AsyncMock(return_value={'title': 'Фильмы', 'text': 'Дюна', 'edit_version': 'v', 'truncated': False}),
                prepare_note_update=AsyncMock(return_value={'message_id': 'old', 'title': 'Фильмы', 'content': 'Дюна\nПрибытие', 'expected_version': 'v'}), update_note=AsyncMock())
            agent = Agent(SimpleNamespace(responses=SimpleNamespace(create=create)), 'test', Calendar(None), store, gmail=gmail)
            _, buttons = await agent.answer('Добавь Прибытие в заметку про фильмы')
            self.assertEqual(len(buttons), 1)
            gmail.prepare_note_update.assert_awaited_once()
            gmail.update_note.assert_not_called()
            self.assertEqual(store.take(buttons[0][0])['action'], 'update_note')
