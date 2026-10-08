# D2Coding 1.4.0

Unmodified Regular and Bold WOFF2 files from
[NAVER's VER1.4.0 release](https://github.com/naver/d2-coding-font/tree/VER1.4.0/site/fonts).
Copyright NAVER Corporation; distributed under the included SIL Open Font License 1.1.

`font_css.py` embeds these files and the license in the generated hub and public
pages, so each HTML file keeps working offline without an installed font or CDN.
These upstream webfonts contain the ligature build. The embedded font-face rules
disable both `calt` and `liga`, giving ordinary code glyphs in every monospace
area, including selectors that do not use the `.mono` class. The UI body font is unchanged.
The optional dotted zero remains disabled.

The upstream files are retained as published:

- `D2Coding-Regular.woff2`: `0fa3ea3b5990782e8e0c124ff6918c64ad057bc87365565405531e010cfdfbaa`
- `D2Coding-Bold.woff2`: `b79b36b1e53e10da5cfc4142c080c755ccb6ae0d359ab4a6335e50184047479a`
