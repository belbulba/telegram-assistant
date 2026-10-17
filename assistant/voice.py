"""Sequential, offline speech recognition in a disposable subprocess."""
import asyncio
import json
import os
from pathlib import Path
import tempfile

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

MAX_SECONDS = 60
MAX_BYTES = 5 * 1024 * 1024


class Voice:
    def __init__(self, folder, python='', model=''):
        self.folder = Path(folder)
        self.python = python
        self.model = model
        self.lock = asyncio.Lock()

    async def transcribe(self, voice):
        if not self.python or not self.model:
            raise ValueError('Распознавание голосовых пока не настроено.')
        if voice.duration > MAX_SECONDS or (voice.file_size or 0) > MAX_BYTES:
            raise ValueError('Пришли голосовое до 60 секунд и размером до 5 МБ.')
        async with self.lock:
            # The context also removes audio on download failure, timeout or cancellation.
            with tempfile.TemporaryDirectory(prefix='voice-', dir=self.folder) as tmp:
                audio = Path(tmp) / 'audio.ogg'
                remote = await voice.get_file()
                if (remote.file_size or 0) > MAX_BYTES:
                    raise ValueError('Голосовое слишком большое: максимум 5 МБ.')
                await remote.download_to_drive(audio)
                if audio.stat().st_size > MAX_BYTES:
                    raise ValueError('Голосовое слишком большое: максимум 5 МБ.')
                env = dict(os.environ, HF_HUB_OFFLINE='1', OMP_NUM_THREADS='1',
                           OPENBLAS_NUM_THREADS='1', HF_HUB_DISABLE_TELEMETRY='1')
                proc = await asyncio.create_subprocess_exec(
                    self.python, str(Path(__file__).with_name('voice_worker.py')),
                    self.model, str(audio),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    env=env)
                try:
                    output, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
                    if proc.returncode:
                        raise ValueError('Не удалось распознать запись. Попробуй записать ещё раз или напиши текстом.')
                    text = json.loads(output)['text'].strip()
                    if not text or len(text) > 16000:
                        raise ValueError('Не удалось разобрать речь. Попробуй записать ещё раз или напиши текстом.')
                    return text
                except asyncio.TimeoutError:
                    raise ValueError('Распознавание заняло слишком долго. Пришли запись покороче или напиши текстом.') from None
                finally:
                    if proc.returncode is None:
                        proc.kill()
                        await proc.wait()


async def receive_voice(message, recognizer, store):
    await message.reply_text('Распознаю голосовое… Затем покажу текст для подтверждения.')
    try:
        text = await recognizer.transcribe(message.voice)
    except (ValueError, OSError, KeyError) as exc:
        # Never expose filenames, tokens or decoder exception contents.
        result = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else 'Не удалось обработать голосовое. Попробуй ещё раз или напиши текстом.'
        await message.reply_text(result)
        return
    key = store.stage({'action': 'voice_text', 'text': text})
    from .main import send
    await send(message, 'Я услышал:\n\n' + text + '\n\nВыполнить этот запрос? Проверь даты, время и имена. Кнопки действуют 10 минут.',
               reply_markup=InlineKeyboardMarkup([[
                   InlineKeyboardButton('Выполнить', callback_data='voice:' + key),
                   InlineKeyboardButton('Отмена', callback_data='voice_no:' + key)]]))
