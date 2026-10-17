"""Official Sign in with ChatGPT: subscription-only OAuth, no API-key fallback."""
import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from openai import AsyncOpenAI
from .config import data_dir

ISSUER = 'https://auth.openai.com'
RESOURCE = 'https://api.openai.com/v1'
TOKEN_URL = ISSUER + '/api/accounts/oauth/token'
PLAN_SCOPE = 'chatgpt.tokens.use.direct'
USAGE_URL = 'https://chatgpt.com/#settings/Usage'


def save_json(path, value):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def validate_callback(query, state, registered_id=None):
    if query.get('state') != [state]:
        raise ValueError('Неверный OAuth state. Авторизация отклонена.')
    if 'error' in query:
        raise ValueError('Вход отменён или ChatGPT не разрешил подключение.')
    client_id = query.get('client_id', [registered_id])[0]
    if not client_id or client_id == 'dynamic_agent_client':
        raise ValueError('ChatGPT не выдал client ID для приложения.')
    if registered_id and client_id != registered_id:
        raise ValueError('OAuth client ID не совпадает с выбранным аккаунтом.')
    code = query.get('code', [None])[0]
    if not code:
        raise ValueError('В ответе отсутствует код авторизации.')
    return client_id, code


def validate_identity(id_token, key, client_id, nonce, subject=None):
    identity = jwt.decode(id_token, key, algorithms=['RS256'], audience=client_id,
        issuer=ISSUER, leeway=60,
        options={'require': ['exp', 'iss', 'aud', 'sub', 'nonce']})
    if identity['nonce'] != nonce:
        raise ValueError('Nonce ID token не совпадает.')
    if subject and subject != identity['sub']:
        raise ValueError('Выполнен вход в другой аккаунт. Прежний сеанс сохранён.')
    return identity


class PlanAuth:
    def __init__(self, folder=None):
        self.folder = folder or data_dir()
        self.path = self.folder / 'chatgpt-session.json'
        self.host_path = self.folder / 'chatgpt-host.json'
        self.lock = asyncio.Lock()

    def read(self):
        if not self.path.exists():
            raise ValueError('Подключите подписку: выполните ./chatgpt-login.command')
        return json.loads(self.path.read_text())

    def record(self, previous, tokens):
        scopes = tokens['scope'].split() if 'scope' in tokens else previous.get('scopes', [])
        if PLAN_SCOPE not in scopes:
            raise ValueError('Доступ к подписке не разрешён. Включите ChatGPT plan usage при входе.')
        if not tokens.get('access_token'):
            raise ValueError('ChatGPT не выдал токен доступа.')
        return {**previous, **tokens, 'scopes': scopes, 'expires_at': time.time() + int(tokens.get('expires_in', 3600))}

    async def token(self):
        async with self.lock:
            session = self.read()
            if PLAN_SCOPE not in session.get('scopes', []):
                raise ValueError('Нет разрешения использовать подписку ChatGPT.')
            if session.get('expires_at', 0) > time.time() + 60:
                return session['access_token']
            if not session.get('refresh_token'):
                raise ValueError('Сеанс ChatGPT истёк. Повторите ./chatgpt-login.command')
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(TOKEN_URL, data={'grant_type': 'refresh_token',
                    'client_id': session['client_id'], 'refresh_token': session['refresh_token'], 'resource': RESOURCE})
            if response.is_error:
                raise ValueError('Не удалось обновить вход ChatGPT. Повторите ./chatgpt-login.command')
            session = self.record(session, response.json())
            save_json(self.path, session)
            return session['access_token']

    def login(self):
        previous = json.loads(self.path.read_text()) if self.path.exists() else {}
        if self.host_path.exists():
            host = json.loads(self.host_path.read_text())['ext_agent_host_id']
        else:
            host = 'urn:uuid:' + str(uuid.uuid4())
            save_json(self.host_path, {'ext_agent_host_id': host})
        state, nonce, verifier = (secrets.token_urlsafe(48) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        result = {}

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Callback contains authorization code: never log URLs.

            def do_GET(self):
                parsed = urlsplit(self.path)
                if parsed.path != '/auth/callback':
                    self.send_error(404)
                    return
                query = parse_qs(parsed.query)
                if query.get('state') != [state]:
                    self.send_error(400, 'Invalid state')
                    return
                result.update(query)
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write('Можно закрыть эту вкладку и вернуться в Terminal.'.encode())

        with HTTPServer(('127.0.0.1', 0), Callback) as server:
            redirect = f'http://127.0.0.1:{server.server_port}/auth/callback'
            registered = previous.get('client_id')
            params = {'client_id': registered or 'dynamic_agent_client', 'ext_agent_host_id': host,
                'response_type': 'code', 'redirect_uri': redirect, 'scope': 'openid profile email offline_access resource.invoke ' + PLAN_SCOPE,
                'resource': RESOURCE, 'state': state, 'nonce': nonce, 'code_challenge_method': 'S256', 'code_challenge': challenge}
            if not registered:
                params['agent_name_hint'] = 'Telegram Personal Assistant'
            print('Continue with ChatGPT — используем только подписку.\nОткройте ссылку в браузере этого Mac и разрешите ChatGPT plan usage:\n', flush=True)
            print(ISSUER + '/api/accounts/authorize?' + urlencode(params), flush=True)
            print('\nВход действует 5 минут. Дополнительные кредиты покупать не требуется.\nУправление лимитом: ' + USAGE_URL, flush=True)
            deadline = time.monotonic() + 300
            server.timeout = 1
            while not result and time.monotonic() < deadline:
                server.handle_request()
        if not result:
            raise ValueError('Время ожидания входа истекло. Запустите команду снова.')
        client_id, code = validate_callback(result, state, registered)
        # Retain registration if code exchange fails, so retry does not register a new app.
        save_json(self.path, {**previous, 'client_id': client_id})
        with httpx.Client(timeout=30) as client:
            response = client.post(TOKEN_URL, data={'grant_type': 'authorization_code', 'client_id': client_id,
                'code': code, 'code_verifier': verifier, 'redirect_uri': redirect, 'resource': RESOURCE})
            if response.is_error:
                raise ValueError('ChatGPT не завершил обмен кода. Повторите вход.')
            tokens = response.json()
            if PLAN_SCOPE not in tokens.get('scope', '').split():
                raise ValueError('ChatGPT plan usage не разрешён. Повторите вход и разрешите использование подписки.')
            metadata_response = client.get(ISSUER + '/.well-known/openid-configuration')
            metadata_response.raise_for_status()
            metadata = metadata_response.json()
            jwks_url = metadata['jwks_uri']
            if urlsplit(jwks_url).scheme != 'https' or urlsplit(jwks_url).hostname != 'auth.openai.com':
                raise ValueError('Неожиданный адрес JWKS OpenAI.')
            jwks_response = client.get(jwks_url)
            jwks_response.raise_for_status()
            id_token = tokens.get('id_token')
            if not id_token:
                raise ValueError('ChatGPT не выдал ID token.')
            header = jwt.get_unverified_header(id_token)
            if header.get('alg') != 'RS256':
                raise ValueError('Неожиданный алгоритм подписи ID token.')
            key_data = next((key for key in jwks_response.json()['keys'] if key.get('kid') == header.get('kid')), None)
            if not key_data:
                raise ValueError('Ключ подписи ID token не найден.')
            key = jwt.PyJWK.from_dict(key_data, algorithm='RS256').key
            identity = validate_identity(id_token, key, client_id, nonce, previous.get('subject'))
        session = self.record(previous, tokens)
        session.update(client_id=client_id, subject=identity['sub'], email=identity.get('email'), issuer=ISSUER)
        save_json(self.path, session)
        print('Подписка ChatGPT подключена. API-ключ не используется.')


class PlanClient:
    def __init__(self, auth=None):
        self.auth = auth or PlanAuth()
        self.responses = self
        self.selected_model = None

    async def model(self, requested=''):
        if self.selected_model:
            return self.selected_model
        token = await self.auth.token()
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(RESOURCE + '/models', headers={'Authorization': 'Bearer ' + token})
        if response.is_error:
            raise ValueError('Не удалось получить модели подписки ChatGPT. Проверьте вход и лимиты.')
        models = [item for item in response.json().get('models', []) if item.get('visibility') == 'list']
        choices = [item['slug'] for item in models]
        if not choices or (requested and requested not in choices):
            raise ValueError('Выбранная модель недоступна подписке. Оставьте CHATGPT_MODEL пустым для автоматического выбора.')
        self.selected_model = requested or choices[0]
        return self.selected_model

    async def create(self, **kwargs):
        model = await self.model(kwargs.pop('model', ''))
        token = await self.auth.token()
        kwargs.update(model=model, store=False, stream=True, include=['reasoning.encrypted_content'])
        async with AsyncOpenAI(api_key=token, base_url=RESOURCE, timeout=90, max_retries=0) as client:
            stream = await client.responses.create(**kwargs)
            output_items = {}
            async with stream:
                async for event in stream:
                    if event.type == 'response.output_item.done':
                        output_items[event.output_index] = event.item
                    if event.type == 'response.completed':
                        if event.response.status != 'completed':
                            break
                        # Plan streams can omit output from the terminal envelope.
                        # Preserve full messages, tool calls and encrypted reasoning.
                        if not event.response.output and output_items:
                            event.response.output = [output_items[i] for i in sorted(output_items)]
                        return event.response
                    if event.type in ('response.failed', 'response.incomplete', 'error'):
                        raise ValueError('ChatGPT не завершил ответ. Проверьте лимит подписки: ' + USAGE_URL)
        raise ValueError('Поток ответа ChatGPT прервался. Повторите запрос; после записи события сначала проверьте Outlook.')

    async def close(self):
        pass


if __name__ == '__main__':
    os.umask(0o077)
    try:
        PlanAuth().login()
    except KeyboardInterrupt:
        print('\nВход отменён.')
        sys.exit(1)
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, jwt.PyJWTError) else type(exc).__name__
        print('Ошибка входа: ' + message, file=sys.stderr)
        sys.exit(1)
