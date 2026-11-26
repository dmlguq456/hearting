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
const backlogBoard = require('./backlog-board.cjs');

const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
// The width rule is a plain one (from U+2E80 up two cells, the rest one): the real popup measures with the terminal's Unicode tables, which
// this script's isolated environment does not have, so a combining mark, a zero-width character or a jamo can differ by a cell.
const cellWidth = text => [...text].reduce((n, c) => n + (c.codePointAt(0) > 0x2e7f ? 2 : 1), 0);
const width = panelView.popupWidth(input.cols, 'table');
if (input.mode === 'size') { console.log(JSON.stringify({ width })); process.exit(0); }

const inner = Math.max(0, width - 4), room = panelView.popupRoom(input.rows, 0);
const names = input.sections, plain = row => row.map(part => part.text).join('');

// One screen: the rows at a scroll, the keys row under them, the frame round them (what the host's `popupNow` does for a table).
function screen(all, scroll, own, reveal, span = 1, typed = null) {
  const pieces = place => [place, own, input.keys.popup_keys.replace('{panel}', input.keys.fold)].filter(Boolean);
  const guideOf = place => panelView.keyRows(pieces(place).flatMap(line => String(line).split(' · ')), inner, { rows: Math.max(1, room - 1), cellWidth });
  let place = '', guide = [], space = 1;
  // (the host's own order: the place, then the keys; a row being typed into on the backlog's board stands above them)
  const lay = () => {
    place = all.length > Math.max(1, room - 2 - (typed ? 1 : 0)) ? `${Math.min(all.length, scroll + 1)}/${all.length}` : '';
    guide = guideOf(place).map(text => [{ text, dim: true }]); if (typed) guide = [typed, ...guide]; space = Math.max(1, room - guide.length);
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
  const body = [...all.slice(scroll, scroll + space), ...guide];
  const title = [{ text: input.word || 'STATUS', dim: true }, { text: input.title_rest }];
  const view = panelView.popup({ cols: input.cols, rows: input.rows, band: 0, kind: 'table', title, body, sections: names, current: input.current || 0,
    model: { colors: 256, no_color: true }, cellWidth, background: '' });
  // The frame alone: the empty cells round it (one a side, one above and below where the window has them) are left out.
  const above = view.frame.top - view.top, left = view.frame.left - view.left, right = view.left + view.width - (view.frame.left + view.frame.width);
  const lines = view.rows.slice(above, above + view.frame.height).map(row => plain(row.slice(left, row.length - right)));
  return { text: lines.join('\n'), scroll };
}

const answer = input.answer, entries = statusList.entriesOf(answer);
// 11-26: the backlog's board (scheme A): the host's own state and keys (`backlog-board.cjs`), each step from a board opened afresh.
// (printed whole: an exit right after a write to a pipe would cut it)
const board = backlogBoard.fromAnswer(answer);
function boardSteps() {
  const shots = [];
  const fresh = () => { const t = { sel: '', fold: new Set(), items: new Set(), more: {}, scroll: 0 }, bs = backlogBoard.newState(); backlogBoard.prepare(board, bs, t); return { t, bs }; };
  const shot = (label, { t, bs }) => {
    const view = statusList.layout(backlogBoard.entries(board, bs, { width: inner, measure: cellWidth }), t, { width: inner, measure: cellWidth });
    const typed = backlogBoard.inputRow(board, bs, inner, cellWidth);
    const s = screen(view.rows, t.scroll || 0, input.keys.board || input.keys.list, view.at, view.span, typed);
    shots.push(`--- ${label} ---\n${s.text}`);
  };
  const typeAll = (st, text) => { for (const ch of Buffer.from(text, 'utf8').toString('latin1')) backlogBoard.type(board, st.bs, st.t, '', ch); };
  const find = (st, text, area) => { backlogBoard.startFind(st.bs); if (area) backlogBoard.type(board, st.bs, st.t, 'tab'); typeAll(st, text); };
  for (const step of input.steps) {
    const st = fresh();
    if (step === 'first') shot('first screen (grouped by kind)', st);
    else if (step === 'kind' || step === 'area' || step === 'date') { backlogBoard.setAxis(board, st.bs, st.t, step); shot(`${step[0]}: grouped by ${step}`, st); }
    else if (step === 'discuss') { backlogBoard.cycleFilter(board, st.bs, st.t); shot('t: only Discuss', st); }
    else if (step === 'typing') { find(st, input.find || ''); shot(`/ ${input.find || ''}: being typed`, st); }
    else if (step === 'find' || step === 'detail') {
      find(st, input.find || ''); backlogBoard.type(board, st.bs, st.t, 'enter');
      if (step === 'detail') {
        const entries = backlogBoard.entries(board, st.bs, { width: inner, measure: cellWidth });
        const key = st.t.sel;
        statusList.press(st.t, statusList.layout(entries, st.t), key);
        if (input.details && input.details[key]) st.t.more[key] = statusList.rowsOf(input.details[key]);
      }
      shot(step === 'find' ? `/ ${input.find || ''} Enter: a title found` : `/ ${input.find || ''} Enter Enter: the item opened`, st);
    } else if (step === 'findarea') { find(st, input.find_area || '', true); backlogBoard.type(board, st.bs, st.t, 'enter'); shot(`/ Tab ${input.find_area || ''} Enter: an area found`, st); }
    else if (step === 'end' || step === 'home') { backlogBoard.ends(board, st.bs, st.t, step === 'end'); shot(step === 'end' ? 'End: the last item' : 'Home: the first item', st); }
    else if (step === 'full') { const s = screen(statusList.rowsOf(input.document || answer), 0, ''); shots.push(`--- full document ---\n${s.text}`); }
  }
  return shots.join('\n\n');
}
const documentRows = statusList.rowsOf(input.document || answer), fleetRows = input.fleet ? statusList.rowsOf(input.fleet) : [];
const state = { sel: '', fold: new Set(), items: new Set(), more: {} };
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
if (board) console.log(boardSteps());
else for (const step of input.steps) {
  if (!entries) { if (step === 'first' || step === 'full') fullScreen(step === 'first' ? 'first screen (no list screen: the document as it is)' : 'full document'); continue; }
  if (step === 'first') listScreen('first screen');
  if (step === 'group') {
    const key = first('g:');
    if (key) { statusList.press(state, statusList.layout(entries, state), key); listScreen('a group opened'); } else out.push('--- a group opened ---\n(this document has no folded group)');
  }
  if (step === 'item') {        state.items.clear(); // the first item whose row was cut: its whole words stand in place of the row
    const key = first('i:', m => m.text);
    if (key) { statusList.press(state, statusList.layout(entries, state), key); if (input.details && input.details[key]) state.more[key] = statusList.rowsOf(input.details[key]); listScreen('an item opened'); } else out.push('--- an item opened ---\n(this document has no item cut at this width)');
  }
  if (step === 'short') {       state.items.clear(); // the first item that fits its row: there is nothing to open, the screen does not change
    const key = first('i:', m => !m.text);
    if (key) { statusList.press(state, statusList.layout(entries, state), key); listScreen('a short item pressed (nothing to open)'); } else out.push('--- a short item pressed ---\n(this document has no item that fits its row)');
  }
  if (step === 'pgdn' || step === 'pgup' || step === 'end') {   // PgDn / PgUp: the chosen line goes a screen on with the window; `end`: as far as it goes
    const view = statusList.layout(entries, state, { width: inner, measure: cellWidth }), space = Math.max(1, room - 3);
    const to = statusList.jump(view, (step === 'pgup' ? -1 : 1) * (step === 'end' ? 1e9 : Math.max(1, space - 1)));
    if (to) state.sel = to;
    listScreen(step === 'end' ? 'the end of the list' : step === 'pgdn' ? 'PgDn' : 'PgUp');
  }
  if (step === 'all') {
    const key = first('a:');
    if (key) { statusList.press(state, statusList.layout(entries, state), key); listScreen('all in progress opened'); }
  }
  if (step === 'full') fullScreen('full document');
}
if (!board) console.log(out.join('\n\n'));
