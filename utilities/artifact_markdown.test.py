#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from artifact_markdown import render_markdown, safe_url


class MarkdownTests(unittest.TestCase):
    def test_blocks_and_fragments(self):
        result = render_markdown('# 제목\n# 제목\n\n**bold** and `code`\n\n- one\n- two\n\n> quote\n\n| a | b |\n|---|---|\n| c | d |\n\n```py\n<x>\n```', '/root')
        for part in ('id="제목"', 'id="제목-1"', '<strong>bold</strong>', '<code>code</code>', '<ul>', '<blockquote>', '<table>', '&lt;x&gt;'):
            self.assertIn(part, result)

    def test_inline_reference_balanced_and_escaping(self):
        result = render_markdown('[한글](a%20b(1).md#frag) ![pic][img]\n\n[img]: ../한글%25.png "title"', '/root')
        self.assertIn('href="a%20b(1).md#frag"', result)
        self.assertIn('src="../한글%25.png"', result)
        self.assertIn('title="title"', result)

    def test_url_boundary_and_html_subset(self):
        self.assertEqual(safe_url('/root/a%20b.md?q=1#x', '/root'), '/a%20b.md?q=1#x')
        for bad in ('/root-other/x', '/etc/passwd', 'javascript:alert(1)', 'file:///etc/passwd', '//evil/x', 'data:text/html,x'):
            self.assertIsNone(safe_url(bad, '/root'))
        result = render_markdown('<audio controls src="media.wav" onplay="evil()"><source src="../a.ogg" type="audio/ogg"></audio>\n<script>alert(1)</script>', '/root')
        self.assertIn('<audio controls src="media.wav">', result)
        self.assertIn('<source src="../a.ogg" type="audio/ogg">', result)
        self.assertNotIn('onplay', result)
        self.assertNotIn('<script>', result)
        self.assertIn('&lt;script&gt;', result)


if __name__ == '__main__': unittest.main()
