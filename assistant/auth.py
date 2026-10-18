import os
import sys
import msal
from .config import data_dir, required

SCOPES = ['https://graph.microsoft.com/Calendars.ReadWrite']
NOTE_SCOPES = ['https://graph.microsoft.com/ShortNotes.ReadWrite']

class Auth:
    def __init__(self):
        self.path = data_dir() / 'msal-cache.json'
        self.cache = msal.SerializableTokenCache()
        if self.path.exists():
            self.cache.deserialize(self.path.read_text())
        tenant = os.getenv('MICROSOFT_TENANT_ID', 'common')
        self.app = msal.PublicClientApplication(required('MICROSOFT_CLIENT_ID'), authority=f'https://login.microsoftonline.com/{tenant}', token_cache=self.cache)

    def save(self):
        if self.cache.has_state_changed:
            tmp = self.path.with_suffix('.tmp')
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(self.cache.serialize())
            os.replace(tmp, self.path)

    def token(self, scopes=None):
        accounts = self.app.get_accounts()
        result = self.app.acquire_token_silent(scopes or SCOPES, account=accounts[0]) if accounts else None
        self.save()
        if not result or 'access_token' not in result:
            raise RuntimeError('Нужна авторизация Outlook: выполните ./authorize.command')
        return result['access_token']

    def login(self, notes=False):
        flow = self.app.initiate_device_flow(scopes=SCOPES + (NOTE_SCOPES if notes else []))
        if 'user_code' not in flow:
            raise RuntimeError('Microsoft не разрешил device flow. Проверьте регистрацию приложения.')
        print(flow['message'], flush=True)
        result = self.app.acquire_token_by_device_flow(flow)
        self.save()
        if 'access_token' not in result:
            raise RuntimeError('Авторизация не завершена: ' + result.get('error', 'unknown'))
        print('Outlook подключён.')

if __name__ == '__main__':
    try:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument('--notes', action='store_true', help='Разрешить календарь и заметки Outlook')
        Auth().login(notes=parser.parse_args().notes)
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
