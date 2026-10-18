import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from assistant import menu


class AgendaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tz = ZoneInfo('Europe/Minsk')
        self.now = datetime(2026, 10, 17, 23, 30, tzinfo=self.tz)
        self.calendar = SimpleNamespace(tz=self.tz, list=AsyncMock(return_value={'events': [], 'truncated': False}))

    async def test_period_boundaries(self):
        cases = {
            'today': ('2026-10-17T00:00:00+03:00', '2026-10-18T00:00:00+03:00'),
            'tomorrow': ('2026-10-18T00:00:00+03:00', '2026-10-19T00:00:00+03:00'),
            'week': ('2026-10-17T23:30:00+03:00', '2026-10-24T23:30:00+03:00'),
        }
        for period, (start, end) in cases.items():
            with self.subTest(period=period):
                text = await menu.agenda(self.calendar, period, self.now)
                self.calendar.list.assert_awaited_with(start=start, end=end)
                self.assertIn('Событий нет.', text)

    async def test_events_are_local_and_titles_remain_literal(self):
        self.calendar.list.return_value = {'events': [
            {'subject': '**Название** <test>', 'start': {'dateTime': '2026-10-18T12:00:00'},
             'end': {'dateTime': '2026-10-18T12:30:00Z'}},
            {'subject': 'Выходной', 'isAllDay': True,
             'start': {'dateTime': '2026-10-18T00:00:00Z'},
             'end': {'dateTime': '2026-10-19T00:00:00Z'}},
        ], 'truncated': True}
        text = await menu.agenda(self.calendar, 'tomorrow', self.now)
        self.assertIn('18.10 15:00 — 15:30', text)
        self.assertIn('**Название** <test>', text)
        self.assertIn('18.10 · весь день', text)
        self.assertIn('Показана часть', text)

    async def test_unknown_period_cannot_query_calendar(self):
        with self.assertRaises(ValueError):
            await menu.agenda(self.calendar, 'unknown', self.now)
        self.calendar.list.assert_not_called()

    def test_menu_removes_keyboard_and_lists_commands(self):
        self.assertTrue(menu.remove_keyboard().remove_keyboard)
        text = menu.command_menu('Europe/Minsk')
        for command in ('/today', '/tomorrow', '/week', '/status', '/usage', '/reset', '/codes', '/menu'):
            self.assertIn(command, text)
        self.assertIn('Europe/Minsk', text)
