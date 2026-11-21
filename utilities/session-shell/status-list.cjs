'use strict';
// The status popup's list screen (11-21, design C: what waits for the person first).
//
// The backend (`session_status.listing_entries`) sends the generated campaign page as entries: each is rows (painted parts, one
// row for a row that can be chosen), and may carry `key` (it can be chosen), `group` (the heading its open state is kept by),
// `when: 'open'` (shown only while that group is open), `alt` (the rows shown instead while it is open) and `text` with `indent` (an
// item's whole words, which its row cuts: wrapped under the row, `indent` cells in, while the person has the item open). This file
// lays those out by the host's state and moves it. It reads
// nothing, asks nothing and starts nothing: a state is `{sel, fold: Set of group names, items: Set of item keys}`.
//
// A row that can be chosen starts with a two-cell slot; the chosen one has `> ` there and stands on the popup's chosen ground
// (`tint`), so the choice is told by a mark as well as by the colour.

// The screen's rows for a state: `{rows, marks: [{key, at, group}], sel, at, fellBack}`. A chosen key that is not on the screen
// (the document was read again without that line, or its group is closed) falls back to the first row that can be chosen.
// `width` (the popup's inside, in cells) and `measure` (the host's cell measure) are what an opened item is wrapped by; a call that
// leaves them out (the keys, which need only the choices) lays out no opened text.
function layout(entries, state, { width = 0, measure = defaultMeasure } = {}) {
  const open = name => state.fold.has(name);
  const rows = [], marks = [];
  for (const entry of Array.isArray(entries) ? entries : []) {
    if (entry.when === 'open' && !open(entry.group)) continue;
    const own = entry.alt && open(entry.group) ? entry.alt : entry.rows;
    if (entry.key) marks.push({ key: entry.key, at: rows.length, group: entry.group || '' });
    for (const row of own) rows.push(row);
    if (width > 0 && entry.text && state.items.has(entry.key)) for (const row of under(entry, width, measure)) rows.push(row);
  }
  const found = marks.find(m => m.key === state.sel), chosen = found || marks[0] || null;
  if (chosen) rows[chosen.at] = rows[chosen.at].map((part, i) => ({ ...part, tint: true, ...(i === 0 ? { text: '> ', cells: 2 } : {}) }));
  return { rows, marks, sel: chosen ? chosen.key : '', at: chosen ? chosen.at : 0, fellBack: !found };
}

const defaultMeasure = text => [...String(text)].reduce((n, c) => n + (c.codePointAt(0) > 0x2e7f ? 2 : 1), 0);

// `text` in rows of `width` cells: a row breaks where the next character would pass it, a space that would begin a row is dropped.
function wrap(text, width, measure) {
  const rows = [];
  let row = '', used = 0;
  for (const ch of String(text)) {
    const cells = measure(ch);
    if (used + cells > width && row !== '') { rows.push(row); row = ''; used = 0; }
    if (ch === ' ' && row === '' && rows.length) continue;
    row += ch; used += cells;
  }
  if (row !== '' || !rows.length) rows.push(row);
  return rows;
}

// An opened item's whole words under its row: in the style of the words' own part, `indent` cells in, none cut.
function under(entry, width, measure) {
  const indent = Math.max(0, Math.min(entry.indent | 0, width - 8)), words = entry.rows[0][entry.rows[0].length - 1] || {};
  const { cells, tint, ...style } = words;
  return wrap(entry.text, Math.max(1, width - indent), measure).map(piece => [{ ...style, text: ' '.repeat(indent) + piece }]);
}

// The key of the row `delta` rows of choices away from the chosen one (the ends stay), or '' where nothing can be chosen.
function neighbour(view, delta) {
  if (!view.marks.length) return '';
  const at = view.marks.findIndex(m => m.key === view.sel);
  const to = Math.max(0, Math.min(view.marks.length - 1, (at < 0 ? 0 : at) + delta));
  return view.marks[to].key;
}

// Enter, or a press, on a row: a heading or `all N ›` opens or closes its group; an item opens or closes its whole text under
// it; `full document ›` is the document read whole. Returns what happened: 'full', 'fold', 'item' or ''.
function press(state, view, key) {
  const mark = view.marks.find(m => m.key === key);
  if (!mark) return '';
  state.sel = key;
  if (key === 'full') return 'full';
  const flip = (set, value) => { if (!set.delete(value)) set.add(value); };
  if (key.startsWith('g:') || key.startsWith('a:')) { flip(state.fold, mark.group); return 'fold'; }
  if (key.startsWith('i:')) { flip(state.items, key); return 'item'; }
  return '';
}

// The presses of the rows, as the table's own hits (a row stands for its whole width).
function hits(view) {
  return view.marks.map(m => ({ row: m.at, left: 0, right: 4096, action: ['status', m.key] }));
}

// The backend's answer as painted parts: `{rows: [[run]], styles}` where a run is `[text, style index, cells]` (the cells as Fleet
// measured them, which the popup lays the row out by).
function runParts(row, styles) {
  return (Array.isArray(row) ? row : []).map(run => ({ text: String(run?.[0] ?? ''), ...(styles[run?.[1]] || {}),
    ...(Number.isInteger(run?.[2]) && run[2] >= 0 && run[2] <= 400 ? { cells: run[2] } : {}) }));
}
function rowsOf(result) {
  const styles = Array.isArray(result.styles) ? result.styles : [];
  return (Array.isArray(result.rows) ? result.rows : []).map(row => runParts(row, styles));
}
// The list screen's entries from the answer's `list` (null where the answer has none: a document that is not the generated page).
function entriesOf(result) {
  const styles = Array.isArray(result.styles) ? result.styles : [];
  if (!Array.isArray(result.list)) return null;
  const rowsIn = v => (Array.isArray(v) ? v : []).map(row => runParts(row, styles));
  return result.list.filter(e => e && typeof e === 'object').slice(0, 2000).map(e => ({ rows: rowsIn(e.rows),
    ...(typeof e.key === 'string' && e.key.length <= 1000 ? { key: e.key } : {}), ...(typeof e.group === 'string' ? { group: e.group } : {}),
    ...(e.when === 'open' ? { when: 'open' } : {}), ...(Array.isArray(e.alt) ? { alt: rowsIn(e.alt) } : {}),
    ...(typeof e.text === 'string' && e.text.length <= 4000 ? { text: e.text, indent: Number.isInteger(e.indent) && e.indent >= 0 && e.indent <= 40 ? e.indent : 0 } : {}) }));
}

module.exports = { layout, neighbour, press, hits, runParts, rowsOf, entriesOf, wrap };
