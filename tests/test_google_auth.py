import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from assistant.google_auth import GoogleAuth, SCOPES, TOKEN_URI, save_credentials


class GoogleAuthTests(unittest.TestCase):
    def test_missing_login(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, 'не подключён'):
                GoogleAuth(folder).token()

    def test_refresh_saves_private_file_and_uses_google_endpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            auth = GoogleAuth(folder)
            auth.path.write_text(json.dumps({'token_uri': 'https://untrusted.invalid'}))
            credentials = Mock(valid=False, token='test')
            credentials.has_scopes.return_value = True
            credentials.to_json.return_value = '{"token":"test"}'
            with patch('assistant.google_auth.Credentials.from_authorized_user_info', return_value=credentials) as factory:
                self.assertEqual(auth.token(), 'test')
            self.assertEqual(factory.call_args.args[0]['token_uri'], TOKEN_URI)
            credentials.refresh.assert_called_once()
            self.assertEqual(auth.path.stat().st_mode & 0o777, 0o600)

    def test_refresh_failure_hides_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            auth = GoogleAuth(folder)
            auth.path.write_text('{}')
            credentials = Mock(valid=False)
            credentials.refresh.side_effect = RuntimeError('secret token')
            with patch('assistant.google_auth.Credentials.from_authorized_user_info', return_value=credentials):
                with self.assertRaises(RuntimeError) as error:
                    auth.token()
            self.assertNotIn('secret', str(error.exception))
            self.assertEqual(auth.path.read_text(), '{}')

    def test_missing_scope_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as folder:
            auth = GoogleAuth(folder)
            auth.path.write_text('{}')
            credentials = Mock()
            credentials.has_scopes.return_value = False
            with patch('assistant.google_auth.Credentials.from_authorized_user_info', return_value=credentials):
                with self.assertRaisesRegex(RuntimeError, 'gmail.modify'):
                    auth.token()
            credentials.refresh.assert_not_called()

    def test_login_pkce_loopback_and_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'client.json'
            path.write_text(json.dumps({'installed': {'client_id': 'test', 'client_secret': 'test'}}))
            flow = Mock()
            flow.run_local_server.return_value.to_json.return_value = '{}'
            with patch('assistant.google_auth.InstalledAppFlow.from_client_config', return_value=flow) as factory:
                GoogleAuth(folder).login(path)
            self.assertTrue(factory.call_args.kwargs['autogenerate_code_verifier'])
            self.assertEqual(factory.call_args.args[1], SCOPES)
            self.assertEqual(flow.run_local_server.call_args.kwargs['host'], '127.0.0.1')
            self.assertEqual(flow.run_local_server.call_args.kwargs['access_type'], 'offline')

    def test_failed_atomic_save_preserves_existing_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'token.json'
            path.write_text('old')
            credentials = Mock()
            credentials.to_json.side_effect = ValueError('failure')
            with self.assertRaises(ValueError):
                save_credentials(path, credentials)
            self.assertEqual(path.read_text(), 'old')
            self.assertEqual(list(Path(folder).iterdir()), [path])
