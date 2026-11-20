"""Markdown backslash escapes, held and restored: the one implementation Fleet's readers share.

A backslash and an ASCII punctuation mark (CommonMark's escape) outside a code span is held as one
private-use character, so no later rule (link, emphasis, comment) reads it as markup; ``restore``
gives the bare mark back. ``explorer.transcript`` (the popup body) and ``collectors.project_status``
(the Fleet project line, the panel STATUS box) both call this.
"""

PUNCT = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
_MARKS = tuple(PUNCT)
_ESCAPED = 0xF0000                   # a protected escape: this plane's private-use character per ASCII mark
_BACK = {_ESCAPED + ord(mark): mark for mark in PUNCT}


def protect(text):
    """``text`` with each Markdown escape outside code spans held as one private character."""
    out, index = [], 0
    while index < len(text):
        char = text[index]
        if char == "\\" and text[index + 1:index + 2] in _MARKS:
            out.append(chr(_ESCAPED + ord(text[index + 1])))
            index += 2
        elif char == "`" and text.find("`", index + 1) > index + 1:
            end = text.find("`", index + 1)
            out.append(text[index:end + 1])      # a code span, backslashes and all
            index = end + 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


def restore(text):
    return text.translate(_BACK)
