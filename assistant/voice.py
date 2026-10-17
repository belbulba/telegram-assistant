"""Sequential, offline speech recognition in a disposable subprocess."""
import asyncio
import json
import os
from pathlib import Path
import tempfile

from telegram.helpers import escape_markdown

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
                    # Isolate sys.path so assistant/calendar.py cannot shadow stdlib calendar.
                    self.python, '-I', str(Path(__file__).with_name('voice_worker.py')),
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


def with_transcript(answer, transcript=None):
    if transcript is None:
        return answer
    return '**Распознано:**\n' + escape_markdown(transcript, version=2) + '\n\n' + answer


async def receive_voice(message, recognizer, respond):
    try:
        text = await recognizer.transcribe(message.voice)
    except (ValueError, OSError, KeyError) as exc:
        # Never expose filenames, tokens or decoder exception contents.
        result = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else 'Не удалось обработать голосовое. Попробуй ещё раз или напиши текстом.'
        await message.reply_text(result)
        return
    await respond(text)
