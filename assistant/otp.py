"""Deterministic forwarding of fresh email verification codes to the owner."""
import asyncio
from email.utils import parseaddr
import logging
import re
import time

from .gmail import GmailError, body_text, headers

log = logging.getLogger(__name__)
CONTEXT = re.compile(
    r'одноразов\w*\s+(?:временн\w*\s+)?(?:код|парол)|временн\w*\s+(?:код|парол)|'
    r'код\w*\s+(?:для\s+)?(?:подтверждени|вход|доступ|провер|безопасност|авторизац)|'
    r'(?:verification|security|authentication|login|sign[ -]?in|one[ -]?time|confirmation)\s+(?:code|password)|'
    r'\b(?:OTP|passcode)\b|(?:ваш|your)\s+(?:(?:одноразовый|temporary)\s+)?(?:код|code)\b', re.I)
CANDIDATE = re.compile(r'(?<![\w])(?:\d{4,8}|\d{3}[ -]\d{3}|[A-Z0-9]{5,10})(?![\w])')


def extract_code(subject, text):
    combined = subject + '\n' + text
    # Dates, URLs and email addresses are not codes.
    combined = re.sub(r'https?://\S+|\S+@\S+|\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b', ' ', combined)
    contexts = list(CONTEXT.finditer(combined))
    contexts = [ctx for ctx in contexts if not re.search(r'скидк|промо|discount|coupon|promo',
                combined[max(0, ctx.start()-25):ctx.end()+25], re.I)]
    if not contexts:
        return None
    candidates = []
    for match in CANDIDATE.finditer(combined):
        value = match.group()
        normalized = value.replace(' ', '').replace('-', '')
        if not any(c.isdigit() for c in normalized):
            continue
        # Avoid unrelated numbers far from the code wording.
        distance = min(max(0, match.start() - ctx.end(), ctx.start() - match.end()) for ctx in contexts)
        if distance <= 100:
            candidates.append((distance, normalized))
    if not candidates:
        return None
    candidates.sort()
    best = candidates[0]
    # Repeated copies of the same code are normal; conflicting nearby codes are ambiguous.
    if any(code != best[1] and distance <= best[0] + 20 for distance, code in candidates):
        return None
    return best[1]


def notification(message):
    if set(message.get('labelIds', [])) & {'SPAM', 'TRASH', 'SENT', 'DRAFT'}:
        return None
    h = headers(message)
    if h.get('x-uniform-type-identifier') == 'com.apple.mail-note':
        return None
    code = extract_code(h.get('subject', ''), body_text(message.get('payload', {})))
    if not code:
        return None
    name, address = parseaddr(h.get('from', ''))
    domain = address.rsplit('@', 1)[-1] if '@' in address else ''
    # Strip formatting/control characters; preserve the sender domain for identification.
    clean = lambda value: ''.join(c for c in value if c.isprintable() and c not in '<>')[:80]
    service = clean(name) or clean(domain) or 'Неизвестный отправитель'
    if domain and domain.casefold() != service.casefold():
        service += ' (' + clean(domain) + ')'
    return f'🔑 {service}\nКод: {code}'


class CodeNotifier:
    def __init__(self, gmail, store, bot, owner, interval=30):
        self.gmail, self.store, self.bot, self.owner = gmail, store, bot, owner
        self.interval = max(15, interval)

    async def poll(self):
        if self.store.setting('otp_enabled') == '0':
            return
        cursor = self.store.setting('otp_history_id')
        if not cursor:
            profile = await self.gmail.request('GET', 'profile')
            self.store.setting('otp_history_id', profile['historyId'])
            self.store.setting('otp_started_at', int(time.time()))
            self.store.setting('otp_last_poll', time.time())
            return  # Never forward existing inbox contents on initial enrollment.
        page_token = None
        for _ in range(20):
            params = {'startHistoryId': cursor, 'historyTypes': 'messageAdded', 'maxResults': 100}
            if page_token:
                params['pageToken'] = page_token
            try:
                result = await self.gmail.request('GET', 'history', params=params)
            except GmailError as exc:
                if exc.status != 404:
                    raise
                # Expired history: establish a fresh baseline rather than replay old codes.
                profile = await self.gmail.request('GET', 'profile')
                self.store.setting('otp_history_id', profile['historyId'])
                self.store.setting('otp_last_poll', time.time())
                log.warning('Gmail history expired; notification baseline reset.')
                return
            seen = set()
            for record in result.get('history', []):
                for added in record.get('messagesAdded', []):
                    message_id = added['message']['id']
                    if message_id in seen:
                        continue
                    seen.add(message_id)
                    await self.process(message_id)
            page_token = result.get('nextPageToken')
            if not page_token:
                self.store.setting('otp_history_id', result.get('historyId', cursor))
                self.store.setting('otp_last_poll', time.time())
                return
        # Deduplication keeps the next poll safe if an unusually large backlog remains.
        records = result.get('history', [])
        if records:
            self.store.setting('otp_history_id', records[-1]['id'])
        log.warning('Gmail history backlog exceeded one poll; will continue later.')

    async def process(self, message_id):
        try:
            message = await self.gmail.get_message(message_id)
        except GmailError as exc:
            if exc.status == 404:
                return
            raise
        timestamp = int(message.get('internalDate', '0')) / 1000
        if timestamp < max(float(self.store.setting('otp_started_at') or 0), time.time() - 900):
            return
        text = notification(message)
        if not text or self.store.setting('otp_enabled') == '0' or not self.store.reserve_otp(message_id):
            return
        try:
            await self.bot.send_message(chat_id=self.owner, text=text, parse_mode=None,
                                        protect_content=True)
        except Exception:
            # Telegram may have accepted the message before a timeout: do not duplicate it.
            self.store.finish_otp(message_id, 'uncertain')
            log.warning('Code notification delivery unconfirmed; automatic repeat suppressed.')
        else:
            self.store.finish_otp(message_id, 'sent')

    async def run(self):
        while True:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # No message content, codes, IDs, addresses or tokens in logs.
                log.warning('Code notification polling failed: %s', type(exc).__name__)
            await asyncio.sleep(self.interval)
