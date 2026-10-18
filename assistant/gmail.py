import asyncio
import base64
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid
from email.header import decode_header, make_header
from html.parser import HTMLParser
import html
import hashlib
import json
import re
from urllib.parse import quote
import uuid

import httpx


class GmailError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f'Gmail вернул HTTP {status}. Успех операции не подтверждён; не повторяйте запись автоматически.')


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'head'):
            self.hidden += 1
        if not self.hidden and tag in ('br', 'div', 'p', 'tr', 'li', 'h1', 'h2'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'head') and self.hidden:
            self.hidden -= 1
        if not self.hidden and tag in ('div', 'p', 'tr', 'li'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def headers(message):
    result = {}
    for item in message.get('payload', {}).get('headers', []):
        try:
            value = str(make_header(decode_header(item['value'])))
        except (ValueError, LookupError):
            value = item['value']
        result[item['name'].lower()] = value
    return result


def body_text(part):
    part_headers = {h['name'].lower(): h['value'] for h in part.get('headers', [])}
    if part.get('filename') or part_headers.get('content-disposition', '').lower().startswith('attachment'):
        return ''
    parts = part.get('parts', [])
    if parts:
        if part.get('mimeType') == 'multipart/alternative':
            ordered = sorted(parts, key=lambda p: p.get('mimeType') != 'text/plain')
            return next((text for p in ordered if (text := body_text(p)).strip()), '')
        return '\n'.join(body_text(p) for p in parts)
    mime = part.get('mimeType', '')
    data = part.get('body', {}).get('data', '')
    if mime not in ('text/plain', 'text/html') or not data:
        return ''
    charset = re.search(r'charset=["\']?([^;\s"\']+)', part_headers.get('content-type', ''), re.I)
    try:
        raw = base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))
        text = raw.decode(charset.group(1) if charset else 'utf-8', errors='replace')
    except (ValueError, LookupError):
        return ''
    if mime == 'text/html':
        parser = PlainHTML()
        parser.feed(text)
        text = ''.join(parser.parts)
    return re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+', '\n\n', text).strip()


def summary(message):
    h = headers(message)
    return {'id': message['id'], 'title': h.get('subject', '(без названия)'),
            'from': h.get('from', ''), 'date': h.get('date', ''),
            'snippet': html.unescape(message.get('snippet', '')),
            'unread': 'UNREAD' in message.get('labelIds', [])}


def note_message(title, content, address, note_uuid=None):
    if not isinstance(title, str) or not title.strip() or '\n' in title or '\r' in title or len(title) > 500:
        raise ValueError('Название заметки: от 1 до 500 символов, без перевода строки.')
    if not isinstance(content, str) or not content.strip() or len(content) > 100000:
        raise ValueError('Текст заметки: от 1 до 100000 символов.')
    message = EmailMessage(policy=SMTP)
    message['Subject'] = title.strip()
    message['From'] = address
    message['Date'] = formatdate(usegmt=True)
    message['Message-ID'] = make_msgid()
    message['X-Uniform-Type-Identifier'] = 'com.apple.mail-note'
    message['X-Universally-Unique-Identifier'] = str(uuid.UUID(note_uuid) if note_uuid else uuid.uuid4()).upper()
    body = html.escape(content).replace('\r\n', '\n').replace('\r', '\n').replace('\n', '<br>')
    message.set_content('<html><body><div>' + html.escape(title.strip()) + '</div><div><br></div><div>' + body + '</div></body></html>', subtype='html', charset='utf-8')
    return base64.urlsafe_b64encode(message.as_bytes()).decode('ascii')


class Gmail:
    def __init__(self, auth):
        self.auth = auth

    async def request(self, method, path, **kwargs):
        token = await asyncio.to_thread(self.auth.token)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.request(method, 'https://gmail.googleapis.com/gmail/v1/users/me/' + path,
                    headers={'Authorization': 'Bearer ' + token}, **kwargs)
        except httpx.TransportError:
            raise RuntimeError('Нет ответа Gmail. Если создавалась заметка, она могла сохраниться. Проверьте Notes перед повтором.') from None
        if response.is_error:
            raise GmailError(response.status_code)
        return response.json() if response.content else {'status': 'completed'}

    @staticmethod
    def note_version(message):
        return hashlib.sha256(json.dumps({'id': message['id'], 'payload': message.get('payload', {})},
                                       sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    async def editable_note(self, message_id):
        message = await self.get_message(message_id)
        label = await self.notes_label()
        if not label or label not in message.get('labelIds', []) or set(message.get('labelIds', [])) & {'TRASH', 'SPAM', 'DRAFT'}:
            raise ValueError('Заметка больше не находится в Notes. Найдите и прочитайте её заново.')
        def has_attachment(part):
            mime = part.get('mimeType', '')
            if (part.get('filename') or part.get('body', {}).get('attachmentId') or
                (mime and not mime.startswith(('text/', 'multipart/')))):
                return True
            if mime == 'text/html':
                data = part.get('body', {}).get('data', '')
                try:
                    source = base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode('utf-8', errors='replace')
                except ValueError:
                    return True
                if re.search(r'<\s*(?:img|svg|video|audio|object|iframe)\b', source, re.I):
                    return True
            return any(has_attachment(p) for p in part.get('parts', []))
        if has_attachment(message.get('payload', {})):
            raise ValueError('Редактирование заметок с вложениями не поддерживается.')
        text = body_text(message.get('payload', {}))
        if not text or len(text) > 20000:
            raise ValueError('Безопасное редактирование доступно для текстовых заметок до 20000 символов после чтения.')
        return message

    async def prepare_note_update(self, message_id, title, content, expected_version):
        note_message(title, content, 'validation@example.invalid')
        if len(content) > 19000:
            raise ValueError('Новый текст заметки должен быть не длиннее 19000 символов.')
        old = await self.editable_note(message_id)
        if self.note_version(old) != expected_version:
            raise ValueError('Заметка изменилась после чтения. Прочитайте её заново перед изменением.')
        return {'message_id': message_id, 'title': title.strip(), 'content': content, 'expected_version': expected_version}

    async def update_note(self, message_id, title, content, expected_version):
        await self.prepare_note_update(message_id, title, content, expected_version)
        old = await self.editable_note(message_id)
        if self.note_version(old) != expected_version:
            raise ValueError('Заметка изменилась после подтверждения. Найдите её заново.')
        h = headers(old)
        identity = h.get('x-universally-unique-identifier')
        if not identity:
            identity = str(uuid.uuid5(uuid.NAMESPACE_URL, 'gmail-note:' + message_id))
        profile = await self.request('GET', 'profile')
        raw = note_message(title, content, profile['emailAddress'], note_uuid=identity)
        identity = str(uuid.UUID(identity)).upper()
        labels = [label for label in old['labelIds'] if label not in {'TRASH', 'SPAM', 'DRAFT', 'INBOX', 'SENT'}]
        # Gmail messages are immutable: insert a replacement before retiring the original.
        result = await self.request('POST', 'messages', json={'raw': raw, 'labelIds': labels})
        if not result.get('id') or result['id'] == message_id:
            raise RuntimeError('Gmail не подтвердил новую версию. Прежняя заметка не удалялась; проверьте Notes перед повтором.')
        saved = {'id': result['id'], 'previous_id': message_id, 'title': title.strip()}
        try:
            replacement = await self.get_message(result['id'])
            note_label = await self.notes_label()
            expected_text = re.sub(r'\n[ \t]*\n(?:[ \t]*\n)+', '\n\n', title.strip() + '\n\n' + content.replace('\r\n', '\n').replace('\r', '\n')).strip()
            if (not note_label or note_label not in replacement.get('labelIds', []) or
                set(replacement.get('labelIds', [])) & {'TRASH', 'SPAM'} or
                headers(replacement).get('x-universally-unique-identifier') != identity or
                headers(replacement).get('subject') != title.strip() or
                body_text(replacement.get('payload', {})) != expected_text):
                raise ValueError('Не удалось проверить содержимое новой версии.')
            current = await self.editable_note(message_id)
            if self.note_version(current) != expected_version:
                raise ValueError('Прежняя заметка изменилась во время сохранения.')
            retired = await self.request('POST', 'messages/' + quote(message_id, safe='') + '/trash')
            if 'TRASH' not in retired.get('labelIds', []):
                raise RuntimeError('Перенос прежней версии в корзину не подтверждён.')
        except (RuntimeError, ValueError):
            return {**saved, 'status': 'partial', 'warning': 'Новая версия записана, но полное завершение изменения не подтверждено. Проверьте Notes и корзину Gmail; повторять изменение автоматически нельзя.'}
        return {**saved, 'status': 'updated'}

    async def labels(self):
        return {'labels': (await self.request('GET', 'labels')).get('labels', [])}

    async def notes_label(self):
        return next((item['id'] for item in (await self.labels())['labels'] if item.get('name') == 'Notes'), None)

    async def get_message(self, message_id, format='full'):
        if not isinstance(message_id, str) or not message_id:
            raise ValueError('Нужен идентификатор найденного письма или заметки.')
        return await self.request('GET', 'messages/' + quote(message_id, safe=''), params={'format': format})

    async def search(self, query, page_token='', notes=False):
        if not isinstance(query, str) or len(query) > 2000 or not isinstance(page_token, str):
            raise ValueError('Некорректный запрос поиска Gmail.')
        label = await self.notes_label()
        if notes and not label:
            return {'items': [], 'next_page_token': ''}
        params = {'q': query if notes else '(' + (query or 'in:anywhere') + ') -label:Notes -in:trash -in:spam', 'maxResults': 10}
        if notes:
            params['labelIds'] = label
        if page_token:
            params['pageToken'] = page_token
        result = await self.request('GET', 'messages', params=params)
        items = []
        for item in result.get('messages', []):
            try:
                message = await self.get_message(item['id'], format='metadata')
            except GmailError as exc:
                if exc.status == 404:
                    continue
                raise
            if ('TRASH' in message.get('labelIds', []) or 'SPAM' in message.get('labelIds', [])):
                continue
            if notes or label not in message.get('labelIds', []):
                items.append(summary(message))
        return {'items': items, 'next_page_token': result.get('nextPageToken', '')}

    async def read(self, message_id, notes=False):
        message = await self.get_message(message_id)
        label = await self.notes_label()
        is_note = bool((label and label in message.get('labelIds', [])) or headers(message).get('x-uniform-type-identifier') == 'com.apple.mail-note')
        if notes != is_note:
            raise ValueError('Это ' + ('заметка; используйте read_note.' if is_note else 'письмо, а не заметка.'))
        text = body_text(message.get('payload', {}))
        return {**summary(message), 'text': text[:20000], 'truncated': len(text) > 20000,
                'labels': message.get('labelIds', []), 'kind': 'note' if is_note else 'email',
                **({'edit_version': self.note_version(message)} if is_note else {})}

    async def modify_email(self, message_id, action, label_name=''):
        message = await self.read(message_id)
        path = 'messages/' + quote(message_id, safe='')
        if action == 'trash_email':
            result = await self.request('POST', path + '/trash')
            if 'TRASH' not in result.get('labelIds', []):
                raise RuntimeError('Gmail не подтвердил перенос в корзину.')
        else:
            body = {'addLabelIds': [], 'removeLabelIds': []}
            if action == 'mark_email_read':
                body['removeLabelIds'] = ['UNREAD']
            elif action == 'archive_email':
                body['removeLabelIds'] = ['INBOX']
            elif action == 'label_email':
                if not isinstance(label_name, str) or not label_name.strip() or len(label_name) > 200 or label_name.strip().casefold() == 'notes':
                    raise ValueError('Укажите почтовый ярлык, отличный от Notes.')
                labels = (await self.labels())['labels']
                label = next((x for x in labels if x['name'] == label_name.strip()), None)
                if label and label.get('type') != 'user':
                    raise ValueError('Для сортировки используйте пользовательский ярлык.')
                if label is None:
                    label = await self.request('POST', 'labels', json={'name': label_name.strip()})
                body['addLabelIds'] = [label['id']]
            else:
                raise ValueError('Неизвестное почтовое действие.')
            result = await self.request('POST', path + '/modify', json=body)
            if not result.get('id'):
                raise RuntimeError('Gmail не подтвердил изменение письма.')
        return {'status': 'completed', 'action': action, 'title': message['title']}

    async def create_note(self, title, content):
        # Validate before requesting private account data or creating labels.
        note_message(title, content, 'validation@example.invalid')
        profile = await self.request('GET', 'profile')
        labels = (await self.request('GET', 'labels')).get('labels', [])
        label = next((item for item in labels if item.get('name') == 'Notes'), None)
        if label is None:
            label = await self.request('POST', 'labels', json={'name': 'Notes', 'labelListVisibility': 'labelShow', 'messageListVisibility': 'show'})
        raw = note_message(title, content, profile['emailAddress'])
        result = await self.request('POST', 'messages', json={'raw': raw, 'labelIds': [label['id']]})
        if not result.get('id'):
            raise RuntimeError('Gmail не подтвердил создание заметки. Проверьте Notes перед повтором.')
        return {'status': 'created', 'id': result['id'], 'title': title.strip(), 'folder': 'Gmail / Notes'}
