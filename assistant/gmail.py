import asyncio
import base64
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid
import html
import uuid

import httpx


def note_message(title, content, address):
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
    message['X-Universally-Unique-Identifier'] = str(uuid.uuid4()).upper()
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
            raise RuntimeError(f'Gmail вернул HTTP {response.status_code}. Успех операции не подтверждён; не повторяйте запись автоматически.')
        return response.json()

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
