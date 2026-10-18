"""Read-only Gmail check; optional single Apple Notes compatibility probe."""
import argparse
import base64
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid
import html
import json
import os
import uuid

import httpx
from .config import data_dir
from .google_auth import GoogleAuth


def check(create_test_note=False):
    folder = data_dir()
    with httpx.Client(timeout=30, headers={'Authorization': 'Bearer ' + GoogleAuth().token()}) as client:
        def request(method, path, **kwargs):
            response = client.request(method, 'https://gmail.googleapis.com/gmail/v1/users/me/' + path, **kwargs)
            if response.is_error:
                raise RuntimeError(f'Gmail HTTP {response.status_code}; операция не подтверждена.')
            return response.json()

        profile = request('GET', 'profile')
        labels = request('GET', 'labels').get('labels', [])
        label = next((item for item in labels if item['name'] == 'Notes'), None)
        print('Gmail API: профиль и ярлыки доступны.', flush=True)
        print('Ярлык Notes: ' + ('найден.' if label else 'пока отсутствует.'), flush=True)
        if not create_test_note:
            return
        marker = folder / 'gmail-note-probe.json'
        if marker.exists():
            print('Тест уже запускался. Повторную заметку не создаю; проверьте iPhone.')
            return
        if label is None:
            label = request('POST', 'labels', json={'name': 'Notes', 'labelListVisibility': 'labelShow', 'messageListVisibility': 'show'})
        title = 'Тест бота — Gmail'
        content = 'Эта заметка создана для проверки синхронизации Telegram-бота с заметками на iPhone.'
        message = EmailMessage(policy=SMTP)
        message['Subject'] = title
        message['From'] = profile['emailAddress']
        message['Date'] = formatdate(localtime=False, usegmt=True)
        message['Message-ID'] = make_msgid()
        message['X-Uniform-Type-Identifier'] = 'com.apple.mail-note'
        message['X-Universally-Unique-Identifier'] = str(uuid.uuid4()).upper()
        message.set_content('<html><body><div>' + html.escape(title) + '</div><div><br></div><div>' + html.escape(content) + '</div></body></html>', subtype='html', charset='utf-8')
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode('ascii')
        # Mark before POST: an interrupted request must not create a second probe.
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'status': 'pending', 'message_id': str(message['Message-ID'])}, stream)
        result = request('POST', 'messages', json={'raw': raw, 'labelIds': [label['id']]})
        if not result.get('id'):
            raise RuntimeError('Gmail не подтвердил запись. Проверьте Notes перед повтором.')
        marker.write_text(json.dumps({'status': 'created', 'id': result['id']}))
        print('Тестовая заметка «Тест бота — Gmail» добавлена. Проверьте раздел Gmail на iPhone.')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--create-test-note', action='store_true')
    args = parser.parse_args()
    try:
        check(args.create_test_note)
    except (RuntimeError, ValueError, OSError, httpx.HTTPError) as exc:
        print('Проверка не завершена. Тип ошибки: ' + type(exc).__name__)
        if isinstance(exc, RuntimeError):
            print(str(exc))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
