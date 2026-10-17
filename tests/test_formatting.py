import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from assistant.formatting import formatted_messages, utf16_length
from assistant.main import send


class FormattingTests(unittest.TestCase):
    def test_styles_links_and_emoji_offsets(self):
        text, entities = formatted_messages('😀 **жирный и *курсив***, ~~старое~~ [сайт](https://example.com)')[0]
        self.assertEqual(text, '😀 жирный и курсив, старое сайт')
        spans = {e.type: e for e in entities}
        self.assertEqual(spans['bold'].offset, 3)
        self.assertEqual(spans['bold'].length, 15)
        self.assertEqual(spans['italic'].length, 6)
        self.assertEqual(spans['text_link'].url, 'https://example.com')
        self.assertIn('strikethrough', spans)

    def test_code_is_literal_and_html_cannot_create_entities(self):
        text, entities = formatted_messages('`**raw** <>&`\n\n```python\nprint("<>& **raw**")  \n```')[0]
        self.assertIn('**raw** <>&', text)
        self.assertIn('print("<>& **raw**")  ', text)
        self.assertEqual([e.type for e in entities], ['code', 'pre'])
        self.assertEqual(entities[1].language, 'python')
        literal, entities = formatted_messages('<b>literal</b> & 2 < 3')[0]
        self.assertEqual(literal, '<b>literal</b> & 2 < 3')
        self.assertEqual(entities, [])

    def test_headings_lists_and_quotes(self):
        text, entities = formatted_messages('# Заголовок\n\n3. один\n4. два\n\n> цитата')[0]
        self.assertIn('3. один\n4. два', text)
        self.assertEqual(entities[0].type, 'bold')
        self.assertEqual(entities[-1].type, 'blockquote')

    def test_code_inside_bold_does_not_overlap_styles(self):
        text, entities = formatted_messages('**до `x < y` после**')[0]
        self.assertEqual(text, 'до x < y после')
        code = next(e for e in entities if e.type == 'code')
        bold = [e for e in entities if e.type == 'bold']
        self.assertEqual(len(bold), 2)
        for entity in bold:
            self.assertTrue(entity.offset + entity.length <= code.offset or
                            entity.offset >= code.offset + code.length)

    def test_long_nested_text_keeps_entities_and_surrogate_pairs(self):
        source = ('😀 слово ' * 1400).rstrip()
        chunks = formatted_messages('**' + source + '**')
        self.assertGreater(len(chunks), 1)
        self.assertEqual(''.join(text for text, _ in chunks), source)
        for text, entities in chunks:
            self.assertLessEqual(utf16_length(text), 4000)
            self.assertEqual(len(entities), 1)
            self.assertEqual(entities[0].offset, 0)
            self.assertEqual(entities[0].length, utf16_length(text))

    def test_long_code_preserves_language_across_messages(self):
        chunks = formatted_messages('```python\n' + 'print("😀")\n' * 700 + '```')
        self.assertGreater(len(chunks), 1)
        for text, entities in chunks:
            self.assertLessEqual(utf16_length(text), 4000)
            self.assertEqual(entities[0].type, 'pre')
            self.assertEqual(entities[0].language, 'python')


class SendingTests(unittest.IsolatedAsyncioTestCase):
    async def test_formatted_response_and_last_chunk_keyboard(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        await send(message, '**' + ('слово ' * 1500).rstrip() + '**', formatted=True, reply_markup='buttons')
        calls = message.reply_text.call_args_list
        self.assertGreater(len(calls), 1)
        self.assertNotIn('reply_markup', calls[0].kwargs)
        self.assertEqual(calls[-1].kwargs['reply_markup'], 'buttons')
        for call in calls:
            self.assertIsNone(call.kwargs['parse_mode'])
            self.assertEqual(call.kwargs['entities'][0].type, 'bold')

    async def test_calendar_titles_are_literal(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        await send(message, '**Событие** <test>')
        self.assertEqual(message.reply_text.call_args.args, ('**Событие** <test>',))
        self.assertEqual(message.reply_text.call_args.kwargs['entities'], [])
