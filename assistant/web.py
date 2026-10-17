"""Render native web-search citations as ordinary clickable Markdown links."""
import re
from urllib.parse import urlsplit
from telegram.helpers import escape_markdown


def source_link(citation):
    url = citation.get('url', '')
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username or parsed.password or any(char in url for char in '\r\n<>')):
            return None
    except (TypeError, ValueError):
        return None
    title = str(citation.get('title') or parsed.hostname)
    return '[' + escape_markdown(title, version=2) + '](<' + url + '>)'


def cited_text(response):
    parts = []
    for item in response.output:
        if item.type != 'message':
            continue
        for block in getattr(item, 'content', []):
            if block.type != 'output_text':
                continue
            text = block.text
            ranges = {}
            extra = []
            for annotation in block.annotations:
                if annotation.type != 'url_citation':
                    continue
                citation = annotation.model_dump(mode='json')
                link = source_link(citation)
                if not link:
                    continue
                start, end = citation.get('start_index'), citation.get('end_index')
                if (type(start) is int and type(end) is int and 0 <= start < end <= len(text)):
                    ranges.setdefault((start, end), []).append(link)
                elif link not in extra:
                    extra.append(link)
            cursor = 0
            pieces = []
            for (start, end), links in sorted(ranges.items()):
                if start < cursor:
                    extra.extend(link for link in links if link not in extra)
                    continue
                pieces.extend((text[cursor:start], ' '.join(dict.fromkeys(links))))
                cursor = end
            pieces.append(text[cursor:])
            # Do not expose internal search references when annotations were absent.
            rendered = re.sub(r'\ue200[^\ue201]*\ue201', '', ''.join(pieces))
            if extra:
                rendered += '\n\nИсточники: ' + ', '.join(extra)
            parts.append(rendered)
    return '\n\n'.join(parts) if parts else response.output_text
