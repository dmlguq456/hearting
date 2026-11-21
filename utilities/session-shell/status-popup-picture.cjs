'use strict';
// The status popup drawn as text, by the product's own code (the one the host uses): `status-list.cjs` lays the list screen out
// by its state and `panel-view.cjs` draws the frame. `utilities/status_popup_picture.py` runs this in an isolated environment on
// the backend's real answer for one document; nothing here reads a file but its one input, asks, writes or starts anything.
//
//   node status-popup-picture.cjs <input.json>     input: {mode: 'size'|'draw', cols, rows, ...}
//   size: prints {width} (the popup's frame width at `cols`), draw: prints the pictures of `steps`.
const fs = require('node:fs');
const path = require('node:path');
const panelView = require('./panel-view.cjs');
const statusList = require('./status-list.cjs');

const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
// The width rule is a plain one (from U+2E80 up two cells, the rest one): the real popup measures with the terminal's Unicode tables, which
// this script's isolated environment does not have, so a combining mark, a zero-width character or a jamo can differ by a cell.
const cellWidth = text => [...text].reduce((n, c) => n + (c.codePointAt(0) > 0x2e7f ? 2 : 1), 0);
const width = panelView.popupWidth(input.cols, 'table');
if (input.mode === 'size') { console.log(JSON.stringify({ width })); process.exit(0); }

const inner = Math.max(0, width - 4), room = panelView.popupRoom(input.rows, 0);
const names = input.sections, plain = row => row.map(part => part.text).join('');

// One screen: the rows at a scroll, the keys row under them, the frame round them (what the host's `popupNow` does for a table).
function screen(all, scroll, own, reveal, span = 1) {
  const pieces = place => [place, own, input.keys.popup_keys.replace('{panel}', input.keys.fold)].filter(Boolean);
  const guideOf = place => panelView.keyRows(pieces(place).flatMap(line => String(line).split(' · ')), inner, { rows: Math.max(1, room - 1), cellWidth });
  let place = '', guide = [], space = 1;
  const lay = () => {
    place = all.length > Math.max(1, room - 2) ? `${Math.min(all.length, scroll + 1)}/${all.length}` : '';
    guide = guideOf(place); space = Math.max(1, room - guide.length);
  };
  const clamp = () => { scroll = Math.max(0, Math.min(scroll, Math.max(0, all.length - space))); };
  lay(); clamp(); lay(); clamp();
  if (reveal !== undefined) {
    for (let pass = 0; pass < 2; pass++) {
      if (reveal < scroll) scroll = reveal < space ? 0 : reveal; else if (reveal + span > scroll + space) scroll = Math.min(reveal, reveal + span - space);
      clamp(); lay();
    }
  }
  clamp(); lay(); clamp();
  const body = [...all.slice(scroll, scroll + space), ...guide.map(text => [{ text, dim: true }])];
  const title = [{ text: 'STATUS', dim: true }, { text: input.title_rest }];
  const view = panelView.popup({ cols: input.cols, rows: input.rows, band: 0, kind: 'table', title, body, sections: names, current: 0,
    model: { colors: 256, no_color: true }, cellWidth, background: '' });
  // The frame alone: the empty cells round it (one a side, one above and below where the window has them) are left out.
  const above = view.frame.top - view.top, left = view.frame.left - view.left, right = view.left + view.width - (view.frame.left + view.frame.width);
  const lines = view.rows.slice(above, above + view.frame.height).map(row => plain(row.slice(left, row.length - right)));
  return { text: lines.join('\n'), scroll };
}

const answer = input.answer, entries = statusList.entriesOf(answer);
const documentRows = statusList.rowsOf(input.document || answer), fleetRows = input.fleet ? statusList.rowsOf(input.fleet) : [];
const state = { sel: '', fold: new Set(), items: new Set() };
const out = [];
let scroll = 0;
const listScreen = label => {
  const view = statusList.layout(entries, state, { width: inner, measure: cellWidth });
  const shot = screen(view.rows, scroll, input.keys.list, view.at, view.span);
  scroll = shot.scroll; out.push(`--- ${label} ---\n${shot.text}`);
};
const fullScreen = label => {
  const shot = screen([...fleetRows, ...documentRows], 0, '');
  out.push(`--- ${label} ---\n${shot.text}`);
};
const first = (prefix, test = () => true) => (statusList.layout(entries, state).marks.find(m => m.key.startsWith(prefix) && test(m)) || {}).key;
for (const step of input.steps) {
  if (!entries) { if (step === 'first' || step === 'full') fullScreen(step === 'first' ? 'first screen (no list screen: the document as it is)' : 'full document'); continue; }
  if (step === 'first') listScreen('first screen');
  if (step === 'group') {
    const key = first('g:');
    if (key) { statusList.press(state, statusList.layout(entries, state), key); listScreen('a group opened'); } else out.push('--- a group opened ---\n(this document has no folded group)');
  }
  if (step === 'item') {        state.items.clear(); // the first item whose row was cut: its whole words stand in place of the row
    const key = first('i:', m => m.text);
    if (key) { statusList.press(state, statusList.layout(entries, state), key); listScreen('an item opened'); } else out.push('--- an item opened ---\n(this document has no item cut at this width)');
  }
  if (step === 'short') {       state.items.clear(); // the first item that fits its row: there is nothing to open, the screen does not change
    const key = first('i:', m => !m.text);
    if (key) { statusList.press(state, statusList.layout(entries, state), key); listScreen('a short item pressed (nothing to open)'); } else out.push('--- a short item pressed ---\n(this document has no item that fits its row)');
  }
  if (step === 'all') {
    const key = first('a:');
    if (key) { statusList.press(state, statusList.layout(entries, state), key); listScreen('all in progress opened'); }
  }
  if (step === 'full') fullScreen('full document');
}
console.log(out.join('\n\n'));
