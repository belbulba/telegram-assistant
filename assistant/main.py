import asyncio
import logging
import os
import sys
import fcntl
from .chatgpt import PlanClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from .config import data_dir, required
from .auth import Auth
from .calendar import Calendar
from .store import Store
from .agent import Agent
from .formatting import formatted_messages

log = logging.getLogger(__name__)

def allowed(update, owner):
    return bool(update.effective_user and update.effective_user.id == owner and update.effective_chat and update.effective_chat.type == 'private')

async def send(message, text, *, formatted=False, **kwargs):
    if formatted:
        chunks = formatted_messages(text)
    else:
        # Escape literal calendar titles and errors from Markdown interpretation.
        from .formatting import utf16_length
        chunks, part, size = [], '', 0
        for char in text:
            width = utf16_length(char)
            if size + width > 4000:
                chunks.append((part, []))
                part, size = '', 0
            part += char
            size += width
        if part:
            chunks.append((part, []))
    for index, (part, entities) in enumerate(chunks):
        await message.reply_text(part, entities=entities, parse_mode=None,
                                 **(kwargs if index == len(chunks) - 1 else {}))

def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    # HTTP debug logs may contain Telegram tokens or calendar content.
    for name in ('httpx', 'httpcore', 'openai', 'msal'):
        logging.getLogger(name).setLevel(logging.WARNING)
    token = required('TELEGRAM_BOT_TOKEN')
    owner = int(required('TELEGRAM_USER_ID'))
    if owner <= 0:
        raise ValueError('TELEGRAM_USER_ID должен быть положительным числом.')
    client = PlanClient()
    client.auth.read()
    folder = data_dir()
    lockfile = (folder / 'bot.lock').open('w')
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ValueError('Бот уже запущен с этой папкой данных.') from None
    store = Store(folder / 'assistant.sqlite3')
    class LazyOutlook:
        def __init__(self):
            self.auth = None

        def token(self):
            if self.auth is None:
                self.auth = Auth()
            return self.auth.token()

    calendar = Calendar(LazyOutlook(), os.getenv('TIMEZONE', 'Europe/Minsk'))
    agent = Agent(client, os.getenv('CHATGPT_MODEL', '').strip(), calendar, store)
    mutex = asyncio.Lock()

    async def start(update, context):
        if allowed(update, owner):
            await update.message.reply_text('Привет! Использую твою подписку ChatGPT. Пиши по-русски. /reset — очистить контекст, /status — подключения, /usage — лимиты подписки. Для создания встречи укажи время и длительность.')

    async def usage(update, context):
        if allowed(update, owner):
            await update.message.reply_text('Используется подписка ChatGPT. Управление лимитами и доступом: https://chatgpt.com/#settings/Usage')

    async def reset(update, context):
        if allowed(update, owner):
            async with mutex:
                store.clear()
            await update.message.reply_text('История и ожидающие подтверждения очищены.')

    async def status(update, context):
        if allowed(update, owner):
            try:
                await asyncio.to_thread(calendar.auth.token)
                await update.message.reply_text('Бот работает. Токен Outlook доступен. OpenAI проверяется при следующем сообщении.')
            except (ValueError, RuntimeError) as exc:
                await update.message.reply_text(str(exc))

    async def message(update, context):
        if not allowed(update, owner):
            return
        if len(update.message.text) > 16000:
            await update.message.reply_text('Сообщение слишком длинное. Разбей его на части до 16000 символов.')
            return
        async with mutex:
            await context.bot.send_chat_action(update.effective_chat.id, 'typing')
            try:
                text, buttons = await agent.answer(update.message.text)
            except ValueError as exc:
                await send(update.message, str(exc))
                return
            await send(update.message, text, formatted=True)
            for key, label in buttons:
                await send(update.message, label + '\nПодтвердить? (10 минут)', reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton('Подтвердить', callback_data='yes:' + key),
                    InlineKeyboardButton('Отмена', callback_data='no:' + key)]]))

    async def confirm(update, context):
        if not allowed(update, owner):
            return
        query = update.callback_query
        await query.answer()
        async with mutex:
            choice, key = query.data.split(':', 1)
            pending = store.take(key)
            if not pending:
                await query.edit_message_text('Подтверждение истекло или уже использовано.')
                return
            if choice == 'no':
                result = 'Действие отменено.'
            else:
                try:
                    await calendar.write(pending['action'], pending['args'], pending['etag'])
                    result = 'Готово: изменение сохранено в Outlook.'
                except (ValueError, RuntimeError) as exc:
                    result = 'Операция не завершена: ' + str(exc)
            store.add([{'role': 'user', 'content': 'Результат нажатия кнопки: ' + result}])
            await query.edit_message_text(result)

    async def error(update, context):
        # Avoid exception strings/tracebacks: request URLs can contain credentials.
        log.error('Ошибка обработчика: %s', type(context.error).__name__)
        if isinstance(update, Update) and allowed(update, owner) and update.effective_message:
            await update.effective_message.reply_text('Ошибка подключения. Проверьте сеть, вход ChatGPT, лимит подписки (/usage) и авторизацию Outlook. После ошибки записи события проверьте Outlook перед повтором. Платный API не используется.')

    async def shutdown(app):
        await client.close()
        store.db.close()

    app = Application.builder().token(token).concurrent_updates(False).post_shutdown(shutdown).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('reset', reset))
    app.add_handler(CommandHandler('status', status))
    app.add_handler(CommandHandler('usage', usage))
    app.add_handler(CallbackQueryHandler(confirm, pattern=r'^(yes|no):[a-f0-9]{32}$'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message))
    app.add_error_handler(error)
    log.info('Запуск long polling; доступ ограничен личным чатом владельца.')
    app.run_polling(allowed_updates=['message', 'callback_query'], drop_pending_updates=False, bootstrap_retries=0)

if __name__ == '__main__':
    try:
        main()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f'Запуск прерван ({type(exc).__name__}). Проверьте .env, сеть и ./check.command.', file=sys.stderr)
        sys.exit(1)
