import os
import sys
import msal
from .config import data_dir, required

SCOPES = ['https://graph.microsoft.com/Calendars.ReadWrite']

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

    def token(self):
        accounts = self.app.get_accounts()
        result = self.app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
        self.save()
        if not result or 'access_token' not in result:
            raise RuntimeError('Нужна авторизация Outlook: выполните ./authorize.command')
        return result['access_token']

    def login(self):
        flow = self.app.initiate_device_flow(scopes=SCOPES)
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
        Auth().login()
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
