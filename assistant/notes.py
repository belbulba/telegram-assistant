import httpx
from .auth import NOTE_SCOPES


class Notes:
    def __init__(self, graph):
        self.graph = graph

    async def create(self, title, content):
        if not isinstance(title, str) or not title.strip():
            raise ValueError('Укажите название заметки.')
        if not isinstance(content, str) or not content.strip():
            raise ValueError('Укажите текст заметки.')
        try:
            result = await self.graph.request('POST', '/me/notes', scopes=NOTE_SCOPES,
                json={'subject': title.strip(), 'body': {'contentType': 'text', 'content': content}})
        except httpx.TransportError:
            raise RuntimeError('Не удалось получить ответ Outlook. Заметка могла сохраниться: проверьте её перед повторным созданием.') from None
        if not result.get('id'):
            raise RuntimeError('Outlook не подтвердил создание заметки. Проверьте заметки перед повтором.')
        return {'status': 'created', 'id': result['id'], 'title': result.get('subject', title.strip())}
