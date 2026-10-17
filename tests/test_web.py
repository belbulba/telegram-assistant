import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from openai.types.responses import Response
from assistant.agent import Agent
from assistant.calendar import Calendar
from assistant.formatting import formatted_messages
from assistant.store import Store
from assistant.web import cited_text


def result(text, annotations, search=False):
    output = []
    if search:
        output.append({'id': 'ws_test', 'type': 'web_search_call', 'status': 'completed',
                       'action': {'type': 'search', 'query': 'Tesla Model Y'}})
    output.append({'id': 'msg_test', 'type': 'message', 'status': 'completed',
                   'role': 'assistant', 'content': [{'type': 'output_text',
                       'text': text, 'annotations': annotations}]})
    return Response.model_validate({'id': 'resp_test', 'object': 'response', 'created_at': 0,
                                   'status': 'completed', 'model': 'test', 'output': output,
                                   'parallel_tool_calls': False, 'tool_choice': 'auto', 'tools': []})


class WebTests(unittest.TestCase):
    def test_search_sources_shown_when_message_omits_annotations(self):
        response = result('Краткий ответ.', [], search=True)
        response.output[0].action = SimpleNamespace(sources=[SimpleNamespace(
            model_dump=lambda **kwargs: {'type': 'url', 'url': 'https://example.com/forecast'})])
        text = cited_text(response)
        self.assertIn('Источники поиска:', text)
        links = [entity.url for _, entities in formatted_messages(text) for entity in entities if entity.type == 'text_link']
        self.assertEqual(links, ['https://example.com/forecast'])

    def test_inline_citation_becomes_clickable_telegram_link(self):
        text = '🚗 Новость. citeturn0search0'
        annotation = {'type': 'url_citation', 'start_index': text.index(''), 'end_index': len(text),
                      'title': 'Tesla [официально]', 'url': 'https://www.tesla.com/modely'}
        rendered = cited_text(result(text, [annotation], search=True))
        chunks = formatted_messages(rendered)
        self.assertNotIn('turn0search0', rendered)
        links = [entity.url for _, entities in chunks for entity in entities if entity.type == 'text_link']
        self.assertEqual(links, ['https://www.tesla.com/modely'])
        self.assertIn('Tesla [официально]', chunks[0][0])

    def test_invalid_offsets_preserve_answer_and_append_source(self):
        annotation = {'type': 'url_citation', 'start_index': 1000, 'end_index': 1001,
                      'title': 'Источник', 'url': 'https://example.com/a(b)'}
        text = cited_text(result('Ответ.', [annotation]))
        self.assertTrue(text.startswith('Ответ.'))
        self.assertIn('Источники:', text)
        links = [entity.url for _, entities in formatted_messages(text) for entity in entities if entity.type == 'text_link']
        self.assertEqual(links, ['https://example.com/a(b)'])

    def test_untrusted_annotation_cannot_create_javascript_link(self):
        annotation = {'type': 'url_citation', 'start_index': 6, 'end_index': 9,
                      'title': 'Источник', 'url': 'javascript:alert(1)'}
        text = cited_text(result('Ответ. citebad', [annotation]))
        self.assertNotIn('javascript', text)
        self.assertNotIn('', text)


class SearchAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_search_and_calendar_tools_coexist_and_history_replays(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'db')
            self.addCleanup(store.db.close)
            response = result('Нашёл официальный сайт Tesla.', [], search=True)
            create = AsyncMock(return_value=response)
            calendar = Calendar(None)
            calendar.write = AsyncMock()
            agent = Agent(SimpleNamespace(responses=SimpleNamespace(create=create)), 'test', calendar, store)
            await agent.answer('Найди Tesla Model Y в интернете')
            tools = create.call_args.kwargs['tools']
            self.assertTrue(any(tool['type'] == 'web_search' for tool in tools))
            self.assertTrue(any(tool.get('name') == 'list_events' for tool in tools))
            calendar.write.assert_not_called()
            self.assertTrue(any(item.get('type') == 'web_search_call' for item in store.history()))
            await agent.answer('А что ещё?')
            self.assertTrue(any(item.get('type') == 'web_search_call' for item in create.call_args.kwargs['input']))
