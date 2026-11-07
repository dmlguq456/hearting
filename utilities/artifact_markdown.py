#!/usr/bin/env python3
"""Small, dependency-free, read-only Markdown renderer for artifact documents."""
from __future__ import annotations

import html
import posixpath
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit


def safe_url(value: str, root: str | Path) -> str | None:
    value = html.unescape(value).strip()
    if any(ord(c) < 32 or ord(c) == 127 or c == '\\' for c in value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() in ('http', 'https', 'mailto'):
        return value
    if parts.scheme or parts.netloc or value.startswith('//'):
        return None
    if parts.path.startswith('/'):
        # A URL beginning with / is an absolute *filesystem* path in reports.
        # Compare components, never a string prefix.
        root_path = posixpath.normpath(str(root))
        decoded = unquote(parts.path)
        normalized = posixpath.normpath(decoded)
        if normalized != root_path and not normalized.startswith(root_path.rstrip('/') + '/'):
            return None
        relative = normalized[len(root_path):].lstrip('/')
        return urlunsplit(('', '', '/' + quote(relative, safe='/'), parts.query, parts.fragment))
    return value


class _SafeHTML(HTMLParser):
    tags = {'a': {'href', 'title'}, 'img': {'src', 'alt', 'title'},
            'audio': {'src', 'controls', 'preload'}, 'source': {'src', 'type'}, 'br': set()}
    void = {'img', 'source', 'br'}

    def __init__(self, root):
        super().__init__(convert_charrefs=True)
        self.root = root
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.tags:
            self.parts.append(html.escape(self.get_starttag_text()))
            return
        clean = []
        for key, value in attrs:
            if key not in self.tags[tag]:
                continue
            if key in ('href', 'src'):
                value = safe_url(value or '', self.root)
                if value is None:
                    continue
            if value is None:
                clean.append(key)
            else:
                clean.append(f'{key}="{html.escape(value, quote=True)}"')
        self.parts.append('<' + tag + (' ' + ' '.join(clean) if clean else '') + '>')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in self.tags and tag not in self.void:
            self.parts.append(f'</{tag}>')
        elif tag not in self.tags:
            self.parts.append(html.escape(f'</{tag}>'))

    def handle_data(self, data):
        self.parts.append(html.escape(data))


def _balanced(text, start, opening, closing):
    depth = 1
    i = start + 1
    while i < len(text):
        if text[i] == '\\' and i + 1 < len(text):
            i += 2
            continue
        if text[i] == opening:
            depth += 1
        elif text[i] == closing:
            depth -= 1
            if not depth:
                return i
        i += 1
    return -1


def _destination(value):
    value = value.strip()
    if value.startswith('<') and '>' in value:
        end = value.index('>')
        return value[1:end], value[end+1:].strip().strip('"\'')
    match = re.match(r'^(.*?)(?:\s+["\'](.*)["\'])?$', value)
    return re.sub(r'\\([()\[\] ])', r'\1', match.group(1)), match.group(2) or ''


def _inline(text, root, refs):
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '\\' and i + 1 < len(text):
            out.append(html.escape(text[i+1])); i += 2; continue
        if ch == '`':
            end = text.find('`', i+1)
            if end >= 0:
                out.append('<code>' + html.escape(text[i+1:end]) + '</code>'); i = end+1; continue
        image = ch == '!' and i+1 < len(text) and text[i+1] == '['
        start = i+1 if image else i
        if text[start:start+1] == '[':
            end = _balanced(text, start, '[', ']')
            if end >= 0:
                dest = None; after = end+1
                if after < len(text) and text[after] == '(':
                    close = _balanced(text, after, '(', ')')
                    if close >= 0:
                        dest = _destination(text[after+1:close]); after = close+1
                elif after < len(text) and text[after] == '[':
                    close = _balanced(text, after, '[', ']')
                    if close >= 0:
                        key = text[after+1:close] or text[start+1:end]
                        dest = refs.get(key.casefold()); after = close+1
                if dest:
                    url = safe_url(dest[0], root)
                    label = text[start+1:end]
                    if url is None:
                        out.append(html.escape(text[i:after]))
                    else:
                        url = html.escape(url, quote=True)
                        title = f' title="{html.escape(dest[1], quote=True)}"' if dest[1] else ''
                        if image:
                            out.append(f'<img src="{url}" alt="{html.escape(label, quote=True)}"{title}>')
                        else:
                            out.append(f'<a href="{url}"{title}>{_inline(label, root, refs)}</a>')
                    i = after; continue
        if ch == '<':
            end = text.find('>', i+1)
            if end >= 0:
                parser = _SafeHTML(root); parser.feed(text[i:end+1]); parser.close()
                out.extend(parser.parts); i = end+1; continue
        found = False
        for marker, tag in (('**', 'strong'), ('__', 'strong'), ('*', 'em'), ('_', 'em')):
            if text.startswith(marker, i):
                end = text.find(marker, i+len(marker))
                if end > i+len(marker):
                    out.append(f'<{tag}>' + _inline(text[i+len(marker):end], root, refs) + f'</{tag}>')
                    i = end+len(marker); found = True; break
        if found: continue
        out.append(html.escape(ch)); i += 1
    return ''.join(out)


def render_markdown(text: str, root: str | Path = '/') -> str:
    """Render a deliberately small Markdown subset, escaping unsupported input."""
    lines = text.splitlines()
    refs = {}
    kept = []
    for line in lines:
        match = re.match(r'^\s*\[([^]]+)\]:\s*(.+?)\s*$', line)
        if match:
            refs[match.group(1).casefold()] = _destination(match.group(2))
        else:
            kept.append(line)
    lines = kept
    body = []
    headings = {}
    i = 0
    inline = lambda s: _inline(s, root, refs)
    while i < len(lines):
        line = lines[i]
        if not line.strip(): i += 1; continue
        fence = re.match(r'^\s*(`{3,}|~{3,})', line)
        if fence:
            mark = fence.group(1); i += 1; code = []
            while i < len(lines) and not re.match(r'^\s*' + re.escape(mark[0]) + '{' + str(len(mark)) + r',}\s*$', lines[i]):
                code.append(lines[i]); i += 1
            if i < len(lines): i += 1
            body.append('<pre><code>' + html.escape('\n'.join(code)) + '</code></pre>'); continue
        h = re.match(r'^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$', line)
        if h:
            title = h.group(2); slug = re.sub(r'[^\w\- ]', '', title.lower()).strip().replace(' ', '-') or 'section'
            count = headings.get(slug, 0); headings[slug] = count+1
            if count: slug += '-' + str(count)
            n = len(h.group(1)); body.append(f'<h{n} id="{html.escape(slug, quote=True)}">{inline(title)}</h{n}>'); i += 1; continue
        if i+1 < len(lines) and '|' in line and re.match(r'^\s*\|?\s*:?-{3,}', lines[i+1]):
            def cells(row): return [c.strip() for c in row.strip().strip('|').split('|')]
            body.append('<table><thead><tr>' + ''.join('<th>'+inline(c)+'</th>' for c in cells(line)) + '</tr></thead><tbody>'); i += 2
            while i < len(lines) and '|' in lines[i] and lines[i].strip():
                body.append('<tr>' + ''.join('<td>'+inline(c)+'</td>' for c in cells(lines[i])) + '</tr>'); i += 1
            body.append('</tbody></table>'); continue
        item = re.match(r'^\s*([-+*]|\d+[.)])\s+(.*)$', line)
        if item:
            ordered = item.group(1)[0].isdigit(); tag = 'ol' if ordered else 'ul'; body.append('<'+tag+'>')
            while i < len(lines):
                m = re.match(r'^\s*([-+*]|\d+[.)])\s+(.*)$', lines[i])
                if not m or m.group(1)[0].isdigit() != ordered: break
                body.append('<li>'+inline(m.group(2))+'</li>'); i += 1
            body.append('</'+tag+'>'); continue
        if re.match(r'^\s*>', line):
            block = []
            while i < len(lines) and re.match(r'^\s*>', lines[i]):
                block.append(re.sub(r'^\s*>\s?', '', lines[i])); i += 1
            body.append('<blockquote>'+render_markdown('\n'.join(block), root)+'</blockquote>'); continue
        paragraph = [line]; i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r'^\s*(?:#{1,6}\s|`{3,}|~{3,}|>|[-+*]\s|\d+[.)]\s)', lines[i]):
            if i+1 < len(lines) and '|' in lines[i] and re.match(r'^\s*\|?\s*:?-{3,}', lines[i+1]): break
            paragraph.append(lines[i]); i += 1
        body.append('<p>' + inline('\n'.join(paragraph)) + '</p>')
    return '\n'.join(body)
