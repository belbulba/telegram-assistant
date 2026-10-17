import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from assistant.chatgpt import PlanAuth, PlanClient, PLAN_SCOPE, validate_callback

class OAuthTests(unittest.TestCase):
    def test_callback_rejects_wrong_state_client_and_denial(self):
        cases = [({'state': ['wrong']}, None),
                 ({'state': ['ok'], 'client_id': ['different'], 'code': ['abc']}, 'registered'),
                 ({'state': ['ok'], 'error': ['access_denied']}, None),
                 ({'state': ['ok'], 'code': ['abc']}, None)]
        for query, client in cases:
            with self.subTest(query=query), self.assertRaises(ValueError):
                validate_callback(query, 'ok', client)
        self.assertEqual(validate_callback({'state': ['ok'], 'code': ['abc'], 'client_id': ['issued']}, 'ok'), ('issued', 'abc'))

    def test_plan_scope_required_and_private_storage(self):
        from assistant.chatgpt import save_json
        with tempfile.TemporaryDirectory() as folder:
            auth = PlanAuth(Path(folder))
            with self.assertRaises(ValueError):
                auth.record({}, {'access_token': 'test', 'scope': 'openid'})
            record = auth.record({}, {'access_token': 'test', 'scope': PLAN_SCOPE, 'expires_in': 60})
            save_json(auth.path, record)
            self.assertEqual(auth.path.stat().st_mode & 0o777, 0o600)
            self.assertIn(PLAN_SCOPE, auth.read()['scopes'])

class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_session_never_uses_api_key(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {'OPENAI_API_KEY': 'must-not-be-used'}):
            auth = PlanAuth(Path(folder))
            with self.assertRaises(ValueError):
                await auth.token()

    async def test_valid_session_and_denied_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            auth = PlanAuth(Path(folder))
            auth.path.write_text(json.dumps({'access_token': 'oauth-only', 'expires_at': time.time() + 300, 'scopes': [PLAN_SCOPE]}))
            self.assertEqual(await auth.token(), 'oauth-only')
            auth.path.write_text(json.dumps({'access_token': 'oauth-only', 'expires_at': time.time() + 300, 'scopes': []}))
            with self.assertRaises(ValueError):
                await auth.token()

    async def test_stream_requires_completed_and_uses_fixed_endpoint(self):
        class Stream:
            def __init__(self, events):
                self.events = events
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def __aiter__(self):
                return self.iterate()
            async def iterate(self):
                for event in self.events:
                    yield event

        for terminal in ('response.completed', 'response.failed', 'interrupted'):
            with self.subTest(terminal=terminal):
                response = SimpleNamespace(status='completed', output_text='готов', output=[])
                events = [] if terminal == 'interrupted' else [SimpleNamespace(type=terminal, response=response)]
                sdk = AsyncMock()
                sdk.responses.create.return_value = Stream(events)
                context = AsyncMock()
                context.__aenter__.return_value = sdk
                auth = SimpleNamespace(token=AsyncMock(return_value='oauth-token'))
                plan = PlanClient(auth)
                plan.selected_model = 'available-model'
                with patch('assistant.chatgpt.AsyncOpenAI', return_value=context) as factory:
                    if terminal == 'response.completed':
                        self.assertIs(await plan.create(input='test', store=True), response)
                    else:
                        with self.assertRaises(ValueError):
                            await plan.create(input='test')
                    self.assertEqual(factory.call_args.kwargs['api_key'], 'oauth-token')
                    self.assertEqual(factory.call_args.kwargs['base_url'], 'https://api.openai.com/v1')
                    self.assertTrue(sdk.responses.create.call_args.kwargs['stream'])
                    self.assertFalse(sdk.responses.create.call_args.kwargs['store'])

    async def test_terminal_envelope_recovers_streamed_output_only_on_success(self):
        from openai.types.responses import Response
        from assistant.chatgpt import RESOURCE
        import httpx
        message = {'id': 'msg_test', 'type': 'message', 'role': 'assistant',
                   'status': 'completed', 'content': [{'type': 'output_text', 'text': '391', 'annotations': []}]}
        for terminal in ('completed', 'failed'):
            events = [dict(type='response.output_item.done', output_index=0, item=message),
                      dict(type='response.' + terminal, response=dict(id='resp_test', object='response',
                           created_at=0, status=terminal, output=[], model='test'))]
            body = ''.join('event: ' + e['type'] + '\ndata: ' + json.dumps(e) + '\n\n' for e in events)
            transport = httpx.MockTransport(lambda request: httpx.Response(200,
                headers={'content-type': 'text/event-stream'}, content=body))
            from openai import AsyncOpenAI
            sdk = AsyncOpenAI(api_key='test', base_url=RESOURCE, http_client=httpx.AsyncClient(transport=transport))
            plan = PlanClient(SimpleNamespace(token=AsyncMock(return_value='oauth-token')))
            plan.selected_model = 'test'
            with patch('assistant.chatgpt.AsyncOpenAI', return_value=sdk):
                if terminal == 'completed':
                    result = await plan.create(input=[{'role': 'user', 'content': 'test'}])
                    self.assertEqual(result.output_text, '391')
                    self.assertEqual(result.output[0].id, 'msg_test')
                else:
                    with self.assertRaises(ValueError):
                        await plan.create(input=[])

class IdentityTests(unittest.TestCase):
    def test_clock_tolerance_keeps_signature_and_identity_checks(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from assistant.chatgpt import validate_identity, ISSUER
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        claims = dict(iss=ISSUER, aud='client', sub='owner', nonce='nonce', iat=now + 5, exp=now + 3600)
        token = jwt.encode(claims, key, algorithm='RS256')
        self.assertEqual(validate_identity(token, key.public_key(), 'client', 'nonce')['sub'], 'owner')
        with self.assertRaises(ValueError):
            validate_identity(token, key.public_key(), 'client', 'wrong')
        with self.assertRaises(jwt.InvalidAudienceError):
            validate_identity(token, key.public_key(), 'other-client', 'nonce')
        with self.assertRaises(jwt.ImmatureSignatureError):
            validate_identity(jwt.encode({**claims, 'iat': now + 300}, key, algorithm='RS256'), key.public_key(), 'client', 'nonce')
        with self.assertRaises(jwt.ExpiredSignatureError):
            validate_identity(jwt.encode({**claims, 'exp': now - 300}, key, algorithm='RS256'), key.public_key(), 'client', 'nonce')
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with self.assertRaises(jwt.InvalidSignatureError):
            validate_identity(token, other.public_key(), 'client', 'nonce')
