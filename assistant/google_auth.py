"""Local Google OAuth enrollment; credentials stay in private DATA_DIR."""
import argparse
import json
import logging
import os
from pathlib import Path
import tempfile
import threading

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from .config import data_dir

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
TOKEN_URI = 'https://oauth2.googleapis.com/token'


def save_credentials(path, credentials):
    fd, temp = tempfile.mkstemp(prefix='.gmail-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(credentials.to_json())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class GoogleAuth:
    def __init__(self, folder=None):
        self.path = (Path(folder) if folder is not None else data_dir()) / 'gmail-token.json'
        self.lock = threading.Lock()

    def token(self):
        with self.lock:
            if not self.path.exists():
                raise RuntimeError('Gmail не подключён. Выполните ./google-login.command.')
            info = json.loads(self.path.read_text(encoding='utf-8'))
            # Token files may be transferred from the local enrollment machine.
            info['token_uri'] = TOKEN_URI
            credentials = Credentials.from_authorized_user_info(info)
            if not credentials.has_scopes(SCOPES):
                raise RuntimeError('Нужен повторный вход Google с правом gmail.modify.')
            if not credentials.valid:
                try:
                    credentials.refresh(Request())
                except Exception:
                    raise RuntimeError('Не удалось обновить доступ Gmail. Проверьте сеть или повторите вход Google.') from None
                save_credentials(self.path, credentials)
            return credentials.token

    def login(self, client_path):
        config = json.loads(Path(client_path).read_text(encoding='utf-8'))
        installed = config.get('installed', {})
        if not installed.get('client_id') or not installed.get('client_secret'):
            raise ValueError('Нужен JSON OAuth-клиента типа Desktop app.')
        config = {'installed': {'client_id': installed['client_id'],
            'client_secret': installed['client_secret'],
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth', 'token_uri': TOKEN_URI}}
        flow = InstalledAppFlow.from_client_config(config, SCOPES, autogenerate_code_verifier=True)
        credentials = flow.run_local_server(host='127.0.0.1', port=0, open_browser=False,
            timeout_seconds=600, prompt='consent', access_type='offline',
            authorization_prompt_message='Откройте на этом компьютере: {url}',
            success_message='Google подключён. Можно закрыть эту вкладку.')
        if not credentials.refresh_token or not credentials.has_scopes(SCOPES):
            raise RuntimeError('Google не выдал постоянный доступ gmail.modify. Повторите вход и подтвердите разрешение.')
        save_credentials(self.path, credentials)
        print('Gmail подключён. Авторизация сохранена в закрытой папке данных.', flush=True)


def main():
    os.umask(0o077)
    for name in ('google', 'google_auth_oauthlib', 'requests_oauthlib', 'oauthlib', 'urllib3'):
        logging.getLogger(name).setLevel(logging.WARNING)
    parser = argparse.ArgumentParser()
    parser.add_argument('--client', default=str(data_dir() / 'google-client.json'))
    args = parser.parse_args()
    try:
        GoogleAuth().login(args.client)
    except (OSError, ValueError, RuntimeError) as exc:
        # OAuth exceptions can embed callback URLs or token responses.
        print('Вход Google не завершён. Проверьте JSON клиента, разрешения и сеть. Тип ошибки: ' + type(exc).__name__)
        return 1
    except Exception as exc:
        print('Вход Google не завершён. Тип ошибки: ' + type(exc).__name__)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
