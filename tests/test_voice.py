import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from assistant.voice import Voice, receive_voice, with_transcript


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.recognizer = Voice(self.root, '/test/python', '/test/model')
        async def download(path):
            Path(path).write_bytes(b'test audio')
        self.remote = SimpleNamespace(file_size=10, download_to_drive=AsyncMock(side_effect=download))
        self.voice = SimpleNamespace(duration=10, file_size=10, get_file=AsyncMock(return_value=self.remote))

    def process(self, code=0, text='Покажи встречи завтра'):
        return SimpleNamespace(returncode=code, communicate=AsyncMock(return_value=(json.dumps({'text': text}).encode(), b'')), kill=lambda: None, wait=AsyncMock())

    async def test_success_offline_and_audio_removed(self):
        with patch('assistant.voice.asyncio.create_subprocess_exec', AsyncMock(return_value=self.process())) as spawn:
            self.assertEqual(await self.recognizer.transcribe(self.voice), 'Покажи встречи завтра')
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(spawn.call_args.kwargs['env']['HF_HUB_OFFLINE'], '1')
        self.assertEqual(spawn.call_args.args[1], '-I')

    async def test_limits_before_download(self):
        for field, value in [('duration', 61), ('file_size', 6 * 1024 * 1024)]:
            original = getattr(self.voice, field)
            setattr(self.voice, field, value)
            with self.assertRaises(ValueError):
                await self.recognizer.transcribe(self.voice)
            setattr(self.voice, field, original)
        self.voice.get_file.assert_not_called()

    async def test_download_failure_removes_partial_audio(self):
        async def fail(path):
            Path(path).write_bytes(b'partial')
            raise OSError('private filename')
        self.remote.download_to_drive.side_effect = fail
        with self.assertRaises(OSError):
            await self.recognizer.transcribe(self.voice)
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_timeout_kills_worker_and_removes_audio(self):
        proc = self.process()
        proc.returncode = None
        from unittest.mock import Mock
        proc.kill = Mock()
        proc.communicate.side_effect = asyncio.TimeoutError
        with patch('assistant.voice.asyncio.create_subprocess_exec', AsyncMock(return_value=proc)):
            with self.assertRaisesRegex(ValueError, 'слишком долго'):
                await self.recognizer.transcribe(self.voice)
        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_decoder_error_and_empty_speech(self):
        for proc in [self.process(code=1), self.process(text=' ')]:
            with patch('assistant.voice.asyncio.create_subprocess_exec', AsyncMock(return_value=proc)):
                with self.assertRaises(ValueError):
                    await self.recognizer.transcribe(self.voice)
            self.assertEqual(list(self.root.iterdir()), [])

    async def test_receipt_immediately_dispatches_text_without_confirmation(self):
        message = SimpleNamespace(voice=self.voice, reply_text=AsyncMock())
        recognizer = SimpleNamespace(transcribe=AsyncMock(return_value='Добавь встречу завтра'))
        respond = AsyncMock()
        await receive_voice(message, recognizer, respond)
        respond.assert_awaited_once_with('Добавь встречу завтра')
        message.reply_text.assert_not_awaited()

    async def test_failed_recognition_does_not_dispatch(self):
        message = SimpleNamespace(voice=self.voice, reply_text=AsyncMock())
        recognizer = SimpleNamespace(transcribe=AsyncMock(side_effect=ValueError('Не удалось разобрать речь.')))
        respond = AsyncMock()
        await receive_voice(message, recognizer, respond)
        respond.assert_not_awaited()

    def test_transcript_and_answer_share_message_without_transcript_markup(self):
        from assistant.formatting import formatted_messages
        text = with_transcript('**Ответ:** готово', '**завтра** [подмена](https://example.com)')
        chunks = formatted_messages(text)
        self.assertEqual(len(chunks), 1)
        rendered, entities = chunks[0]
        self.assertIn('**завтра** [подмена](https://example.com)', rendered)
        self.assertIn('Ответ: готово', rendered)
        self.assertFalse(any(entity.type == 'text_link' for entity in entities))

    async def test_concurrent_requests_run_one_worker_at_a_time(self):
        active = 0
        peak = 0
        async def spawn(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            proc = self.process()
            async def communicate():
                nonlocal active
                await asyncio.sleep(0.01)
                active -= 1
                return b'{"text":"test"}', b''
            proc.communicate = communicate
            return proc
        with patch('assistant.voice.asyncio.create_subprocess_exec', spawn):
            await asyncio.gather(self.recognizer.transcribe(self.voice), self.recognizer.transcribe(self.voice))
        self.assertEqual(peak, 1)
        self.assertEqual(list(self.root.iterdir()), [])
