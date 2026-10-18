import asyncio
import logging
import os
import sys
import fcntl
from contextlib import suppress
from datetime import datetime
from .chatgpt import PlanClient
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from telegram.helpers import escape_markdown
from .config import data_dir, required
from .auth import Auth
from .calendar import Calendar
from .store import Store
from .agent import Agent
from .gmail import Gmail
from .google_auth import GoogleAuth
from .otp import CodeNotifier, POLL_INTERVAL
from .formatting import formatted_messages
from . import menu
from .voice import Voice, receive_voice, with_transcript

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


async def apply_confirmation(pending, calendar, gmail):
    if pending['action'] == 'trash_email':
        if not gmail:
            raise RuntimeError('Gmail не подключён.')
        await gmail.modify_email(action='trash_email', **pending['args'])
        return 'Готово: письмо перемещено в корзину Gmail.'
    if pending['action'] not in ('update_event', 'delete_event'):
        raise ValueError('Неизвестное подтверждение.')
    await calendar.write(pending['action'], pending['args'], pending['etag'])
    return 'Готово: изменение сохранено в Outlook.'

def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    # HTTP debug logs may contain Telegram tokens or calendar content.
    for name in ('httpx', 'httpcore', 'openai', 'msal', 'google.auth', 'urllib3'):
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
    persona_path = folder / 'persona.txt'
    persona = persona_path.read_text(encoding='utf-8') if persona_path.exists() else ''
    gmail = Gmail(GoogleAuth()) if (folder / 'gmail-token.json').is_file() else None
    agent = Agent(client, os.getenv('CHATGPT_MODEL', '').strip(), calendar, store, persona=persona, gmail=gmail)
    mutex = asyncio.Lock()
    voice_recognizer = Voice(folder, os.getenv('VOICE_PYTHON', ''), os.getenv('VOICE_MODEL', ''))
    notifier_task = None

    async def start(update, context):
        if allowed(update, owner):
            await update.message.reply_text('Привет! Пиши мне, присылай голосовое до минуты или выбирай действие в меню. Голосовое обработаю сразу: пришлю расшифровку вместе с ответом. Использую твою подписку ChatGPT, веб-поиск и календарь Outlook. Время — ' + calendar.tz.key + '. /menu — показать кнопки, /reset — очистить контекст.', reply_markup=menu.keyboard())

    async def usage(update, context):
        if allowed(update, owner):
            await update.message.reply_text('Используется подписка ChatGPT. Точный остаток и время обновления лимита мне недоступны. Посмотреть использование и лимит приложения можно в настройках ChatGPT. Просмотр календаря кнопками меню не расходует запросы к модели.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Открыть лимиты ChatGPT', url='https://chatgpt.com/#settings/Usage')]]))

    async def schedule(update, context, period=None):
        if not allowed(update, owner):
            return
        period = period or {'/today': 'today', '/tomorrow': 'tomorrow', '/week': 'week'}[update.message.text.split()[0].split('@')[0]]
        async with mutex:
            try:
                text = await menu.agenda(calendar, period)
            except (ValueError, RuntimeError) as exc:
                text = str(exc)
            await send(update.message, text, reply_markup=menu.keyboard())

    async def reset(update, context):
        if allowed(update, owner):
            async with mutex:
                store.clear()
            await update.message.reply_text('История и ожидающие подтверждения очищены.')

    async def status(update, context):
        if allowed(update, owner):
            try:
                await asyncio.to_thread(calendar.auth.token)
                await update.message.reply_text('Бот работает. Токен Outlook доступен. Gmail: ' + ('подключён' if gmail else 'не подключён') + '. OpenAI проверяется при следующем сообщении. /codes — статус уведомлений с кодами.')
            except (ValueError, RuntimeError) as exc:
                await update.message.reply_text(str(exc))

    async def codes(update, context):
        if not allowed(update, owner):
            return
        if not gmail:
            await send(update.message, 'Gmail не подключён.')
            return
        option = context.args[0].lower() if context.args else ''
        if option not in ('', 'on', 'off'):
            await send(update.message, '/codes — статус; /codes on — включить; /codes off — выключить.')
            return
        if option:
            store.setting('otp_enabled', '1' if option == 'on' else '0')
            # A new baseline avoids replaying codes received while disabled.
            if option == 'on':
                store.setting('otp_history_id', '')
                store.setting('otp_started_at', datetime.now().timestamp())
        enabled = store.setting('otp_enabled') != '0'
        last_ok = store.setting('otp_last_poll')
        health = 'Последняя успешная проверка: ' + datetime.fromtimestamp(float(last_ok), calendar.tz).strftime('%d.%m %H:%M:%S') if last_ok else 'Успешных проверок пока не было.'
        await send(update.message, (f'Уведомления с одноразовыми кодами включены. Проверка примерно раз в {POLL_INTERVAL} секунд.' if enabled else 'Уведомления с кодами выключены.') + '\n' + health + '\nКоды приходят только в этот личный чат. Старые письма не пересылаются.')

    async def message(update, context):
        if not allowed(update, owner):
            return
        selected = update.message.text
        if selected in menu.PERIODS:
            await schedule(update, context, menu.PERIODS[selected])
            return
        if selected == menu.USAGE:
            await usage(update, context)
            return
        if selected == menu.STATUS:
            await status(update, context)
            return
        if selected == menu.ADD:
            await update.message.reply_text('Напиши название, дату, время и длительность встречи. Например:\n\nДобавь завтра встречу с Антоном с 15:00 до 15:30, напомни за 15 минут.\n\nЕсли чего-то не хватает, я уточню. Время — ' + calendar.tz.key + '.', reply_markup=menu.keyboard())
            return
        if len(update.message.text) > 16000:
            await update.message.reply_text('Сообщение слишком длинное. Разбей его на части до 16000 символов.')
            return
        async with mutex:
            await answer(update.message, update.message.text, context)

    async def answer(target, request, context, transcript=None):
        await context.bot.send_chat_action(target.chat_id, 'typing')
        try:
            text, buttons = await agent.answer(request)
        except ValueError as exc:
            if transcript is None:
                await send(target, str(exc))
            else:
                await send(target, with_transcript(escape_markdown(str(exc), version=2), transcript), formatted=True)
            return
        await send(target, with_transcript(text, transcript), formatted=True, reply_markup=menu.keyboard())
        for key, label in buttons:
            await send(target, label + '\nПодтвердить? (10 минут)', reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton('Подтвердить', callback_data='yes:' + key),
                InlineKeyboardButton('Отмена', callback_data='no:' + key)]]))

    async def voice_message(update, context):
        if allowed(update, owner):
            async with mutex:
                await context.bot.send_chat_action(update.effective_chat.id, 'typing')
                async def respond(text):
                    await answer(update.message, text, context, transcript=text)
                await receive_voice(update.message, voice_recognizer, respond)

    async def voice_confirm(update, context):
        if not allowed(update, owner):
            return
        query = update.callback_query
        await query.answer()
        async with mutex:
            choice, key = query.data.split(':', 1)
            pending = store.take(key)
            if not pending or pending.get('action') != 'voice_text':
                await query.edit_message_reply_markup(reply_markup=None)
                await send(query.message, 'Подтверждение истекло или уже использовано.')
                return
            await query.edit_message_reply_markup(reply_markup=None)
            if choice == 'voice_no':
                await send(query.message, 'Голосовой запрос отменён.')
                return
            await answer(query.message, pending['text'], context)

    async def confirm(update, context):
        if not allowed(update, owner):
            return
        query = update.callback_query
        await query.answer()
        async with mutex:
            choice, key = query.data.split(':', 1)
            pending = store.take(key)
            if not pending or pending.get('action') not in ('update_event', 'delete_event', 'trash_email'):
                await query.edit_message_text('Подтверждение истекло или уже использовано.')
                return
            if choice == 'no':
                result = 'Действие отменено.'
            else:
                try:
                    result = await apply_confirmation(pending, calendar, gmail)
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
        await stop_background(app)
        await client.close()
        store.db.close()

    async def stop_background(app):
        if notifier_task:
            notifier_task.cancel()
            with suppress(asyncio.CancelledError):
                await notifier_task

    async def setup_menu(app):
        nonlocal notifier_task
        await app.bot.set_my_commands([
            BotCommand('menu', 'Показать кнопки'),
            BotCommand('today', 'События на сегодня'),
            BotCommand('tomorrow', 'События на завтра'),
            BotCommand('week', 'Ближайшие 7 дней'),
            BotCommand('status', 'Подключения'),
            BotCommand('usage', 'Лимиты подписки'),
            BotCommand('reset', 'Очистить историю диалога'),
            BotCommand('codes', 'Уведомления с кодами: статус, on, off'),
        ])
        if gmail:
            notifier_task = asyncio.create_task(CodeNotifier(gmail, store, app.bot, owner).run(), name='gmail-codes')

    app = Application.builder().token(token).concurrent_updates(False).post_init(setup_menu).post_stop(stop_background).post_shutdown(shutdown).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('menu', start))
    app.add_handler(CommandHandler(['today', 'tomorrow', 'week'], schedule))
    app.add_handler(CommandHandler('reset', reset))
    app.add_handler(CommandHandler('status', status))
    app.add_handler(CommandHandler('usage', usage))
    app.add_handler(CommandHandler('codes', codes))
    app.add_handler(CallbackQueryHandler(confirm, pattern=r'^(yes|no):[a-f0-9]{32}$'))
    app.add_handler(CallbackQueryHandler(voice_confirm, pattern=r'^(voice|voice_no):[a-f0-9]{32}$'))
    app.add_handler(MessageHandler(filters.VOICE, voice_message))
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
