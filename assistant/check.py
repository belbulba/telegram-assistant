"""Read-only live diagnostics. Never prints tokens or response bodies."""
import asyncio
import sys
from .chatgpt import PlanClient
from telegram import Bot
from .auth import Auth
from .calendar import Calendar
from .config import required
import os

async def main():
    failed = False
    for name, check in [('Telegram', telegram), ('OpenAI', openai), ('Outlook', outlook)]:
        try:
            await check()
            print(name + ': OK')
        except Exception as exc:
            failed = True
            print(name + ': ошибка (' + type(exc).__name__ + '). Проверьте настройки и сеть.')
    return 1 if failed else 0

async def telegram():
    async with Bot(required('TELEGRAM_BOT_TOKEN')) as bot:
        await bot.get_me()

async def openai():
    client = PlanClient()
    response = await client.responses.create(model=os.getenv('CHATGPT_MODEL', '').strip(), input=[{'role': 'user', 'content': 'Ответь одним словом: готов.'}], max_output_tokens=512, store=False)
    if not response.output_text.strip():
        raise ValueError('ChatGPT вернул пустой ответ.')

async def outlook():
    await Calendar(Auth()).request('GET', '/me/calendar', params={'$select': 'id,name'})

if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
