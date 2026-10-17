"""Render Markdown as Telegram text/entities, without interpreting raw HTML."""
from bisect import bisect_right

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode
from telegram import MessageEntity


def utf16_length(text):
    return len(text.encode('utf-16-le')) // 2


def formatted_messages(markdown, limit=4000):
    """Split rendered text while clipping entity offsets in Telegram UTF-16 units."""
    if limit < 2:
        raise ValueError('Message limit must be at least 2.')
    parser = MarkdownIt('commonmark', {'html': False}).enable('strikethrough')
    root = SyntaxTreeNode(parser.parse(markdown))
    parts, entities = [], []
    position = 0

    def append(text):
        nonlocal position
        parts.append(text)
        position += utf16_length(text)

    def contents(node, quote_depth=0):
        for child in node.children or []:
            render(child, quote_depth)

    def span(kind, node, quote_depth=0, **attributes):
        start = position
        contents(node, quote_depth)
        if position > start:
            entities.append(MessageEntity(kind, start, position - start, **attributes))

    def render(node, quote_depth=0):
        kind = node.type
        if kind == 'text':
            append(node.content)
        elif kind in ('softbreak', 'hardbreak'):
            append('\n')
        elif kind in ('code_inline', 'fence', 'code_block'):
            start = position
            append(node.content)
            if position > start:
                attributes = {}
                if kind == 'fence' and node.info.strip():
                    attributes['language'] = node.info.strip().split()[0]
                entities.append(MessageEntity('code' if kind == 'code_inline' else 'pre',
                                              start, position - start, **attributes))
            if kind != 'code_inline':
                append('\n')
        elif kind in ('strong', 'em', 's', 'heading'):
            span({'strong': 'bold', 'em': 'italic', 's': 'strikethrough', 'heading': 'bold'}[kind], node, quote_depth)
            if kind == 'heading':
                append('\n\n')
        elif kind == 'link':
            span('text_link', node, quote_depth, url=node.attrGet('href'))
        elif kind == 'image':
            contents(node, quote_depth)
        elif kind == 'paragraph':
            contents(node, quote_depth)
            append('\n' if node.parent and node.parent.type == 'list_item' else '\n\n')
        elif kind in ('bullet_list', 'ordered_list'):
            number = int(node.attrGet('start') or 1)
            for index, child in enumerate(node.children or []):
                append(f'{number + index}. ' if kind == 'ordered_list' else '• ')
                contents(child, quote_depth)
            append('\n')
        elif kind == 'blockquote':
            if quote_depth:
                contents(node, quote_depth + 1)
            else:
                span('blockquote', node, quote_depth + 1)
        elif kind == 'hr':
            append('────────\n\n')
        else:
            contents(node, quote_depth)

    contents(root)
    text = ''.join(parts).rstrip('\n')
    if not text:
        text = markdown or 'Пустой ответ.'
        entities = []

    # Telegram forbids styling/link entities from overlapping code entities.
    code_ranges = sorted((e.offset, e.offset + e.length) for e in entities
                         if e.type in ('code', 'pre'))
    normalized = []
    for entity in entities:
        if entity.type in ('code', 'pre'):
            normalized.append(entity)
            continue
        segments = [(entity.offset, entity.offset + entity.length)]
        for left, right in code_ranges:
            segments = [(a, b) for start, end in segments
                        for a, b in ((start, min(end, left)), (max(start, right), end)) if a < b]
        normalized.extend(MessageEntity(entity.type, a, b - a, url=entity.url)
                          for a, b in segments)
    entities = normalized

    # Map Python character indices to UTF-16 offsets, including emoji pairs.
    offsets = [0]
    for char in text:
        offsets.append(offsets[-1] + utf16_length(char))
    chunks = []
    start = 0
    while start < len(text):
        end = bisect_right(offsets, offsets[start] + limit) - 1
        if end < len(text):
            newline = text.rfind('\n', start + (end - start) // 2, end)
            if newline >= 0:
                end = newline + 1
        left, right = offsets[start], offsets[end]
        clipped = []
        for entity in entities:
            a, b = max(left, entity.offset), min(right, entity.offset + entity.length)
            if a < b:
                clipped.append(MessageEntity(entity.type, a - left, b - a,
                                             url=entity.url, language=entity.language))
        chunks.append((text[start:end], sorted(clipped, key=lambda e: (e.offset, -e.length))))
        start = end
    return chunks
