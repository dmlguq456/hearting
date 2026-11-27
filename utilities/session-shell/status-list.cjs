'use strict';
// The status popup's list screen (11-21, design C; since 11-22 no entry is a question: what is in progress stands first).
//
// The backend (`session_status.listing_entries`) sends the generated campaign page as entries: each is rows (painted parts, one
// row for a row that can be chosen), and may carry `key` (it can be chosen), `group` (the id of the heading its open state is kept by),
// `when: 'open'` (shown only while that group is open), `alt` (the rows shown instead while it is open) and `text` (the whole
// words of an item whose row was cut: set in place of that row, wrapped, while the person has the item open; an item whose
// row was not cut has none, so there is nothing to open) and `more` (rows that stand under the item's row while the person has it
// open: the backlog's indented lines; an item that has them can be opened as one whose row was cut can) or `lazy` (the same, the lines being asked for
// when it is opened). Keys and ids are short names, never the words themselves. This file
// lays those out by the host's state and moves it. It reads
// nothing, asks nothing and starts nothing: a state is `{sel, fold: Set of group ids, items: Set of item keys}`.
//
// A row that can be chosen starts with a two-cell slot; the chosen one has `> ` there and stands on the popup's chosen ground
// (`tint`), so the choice is told by a mark as well as by the colour.

// The screen's rows for a state: `{rows, marks: [{key, at, rows, group, text, open}], sel, at, span, fellBack}` (`at` and `span`: where the chosen line starts and how many rows it has). A chosen key that is not on the
// screen (the document was read again without that line, or its group is closed) falls back to the first row that can be chosen.
// `width` (the popup's inside, in cells) and `measure` (the host's cell measure) are what an opened item is wrapped by; a call that
// leaves them out (the keys, which need only the choices) lays out no opened text.
function layout(entries, state, { width = 0, measure = defaultMeasure } = {}) {
  const open = id => state.fold.has(id);
  const rows = [], marks = [];
  for (const entry of Array.isArray(entries) ? entries : []) {
    if (entry.when === 'open' && !open(entry.group)) continue;
    const openable = !!entry.text || !!entry.more || !!entry.lazy;
    const opened = !!entry.key && openable && state.items.has(entry.key);
    const shown = opened && width > 0 && entry.text ? unfold(entry, width, measure) : entry.alt && open(entry.group) ? entry.alt : entry.rows;
    // `lazy`: the lines under the item are not in the answer (the backlog's long notes); the host asks for them when the item is opened and keeps
    // them in `state.more[key]` (until they come, one dim `⠏` stands under the row).
    const later = opened && entry.lazy ? (state.more && state.more[entry.key]) || [[{ text: '    ⠏', dim: true }]] : null;
    const below = entry.more || later;
    const own = opened && below ? [...shown, ...below] : shown;
    if (entry.key) marks.push({ key: entry.key, at: rows.length, rows: own.length, group: entry.group || '', text: openable, open: opened, lazy: !!entry.lazy });
    for (const row of own) rows.push(row);
  }
  const found = marks.find(m => m.key === state.sel), chosen = found || marks[0] || null;
  if (chosen) {
    for (let i = 0; i < chosen.rows; i++) {
      rows[chosen.at + i] = rows[chosen.at + i].map((part, j) => ({ ...part, tint: true, ...(i === 0 && j === 0 ? { text: '▶ ', cells: 2 } : {}) }));
    }
  }
  return { rows, marks, sel: chosen ? chosen.key : '', at: chosen ? chosen.at : 0, span: chosen ? chosen.rows : 0, fellBack: !found };
}

// The width of the host's own measure is the host's (the Unicode width tables of the terminal); this stand-in, for the callers that
// have none, counts every character from U+2E80 up as two cells and the rest as one (a combining mark or a zero-width character is
// counted as one cell, where the host counts none).
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

// An opened item: its one cut row becomes the whole words in rows of the width, the row's own lead (the slot, the `!`) on the first
// and the words' own column kept on the rows after it. The words keep the style of the part they were in.
function unfold(entry, width, measure) {
  const row = entry.rows[0], words = row[row.length - 1] || {}, lead = row.slice(0, -1);
  const { cells, tint, text, ...style } = words;
  const used = lead.reduce((n, part) => n + (Number.isInteger(part.cells) ? part.cells : measure(part.text)), 0);
  const pieces = wrap(entry.text, Math.max(1, width - used), measure);
  return pieces.map((piece, i) => i === 0 ? [...lead, { ...style, text: piece }] : [{ text: ' '.repeat(used) }, { ...style, text: piece }]);
}

// The key of the line a screen away: the first line that can be chosen at or past `delta` rows from the chosen one (up: at or before it), the ends staying
// (PgUp/PgDn: the chosen line goes with the window, so the line Enter opens is the one in view). '' where nothing can be chosen.
function jump(view, delta) {
  const marks = view.marks;
  if (!marks.length) return '';
  const at = Math.max(0, marks.findIndex(m => m.key === view.sel)), goal = marks[at].at + delta;
  let to = at;
  if (delta > 0) { while (to < marks.length - 1 && marks[to].at < goal) to++; } else { while (to > 0 && marks[to].at > goal) to--; }
  return marks[to].key;
}

// The key of the row `delta` rows of choices away from the chosen one (the ends stay), or '' where nothing can be chosen.
function neighbour(view, delta) {
  if (!view.marks.length) return '';
  const at = view.marks.findIndex(m => m.key === view.sel);
  const to = Math.max(0, Math.min(view.marks.length - 1, (at < 0 ? 0 : at) + delta));
  return view.marks[to].key;
}

// Enter, or a press, on a row: a heading or `all N ▸` opens or closes its group; a cut item opens or closes its whole text in place
// of its row (an item that was not cut has nothing to open); `full document ›` is the document read whole. The row pressed is the
// one chosen. Returns what happened: 'full', 'fold', 'item', 'choose' (only the choice moved) or ''.
function press(state, view, key) {
  const mark = view.marks.find(m => m.key === key);
  if (!mark) return '';
  const moved = state.sel !== key;
  state.sel = key;
  if (key === 'full') return 'full';
  const flip = (set, value) => { if (!set.delete(value)) set.add(value); };
  if (key.startsWith('g:') || key.startsWith('a:')) { flip(state.fold, mark.group); return 'fold'; }
  if (key.startsWith('i:') && mark.text) { flip(state.items, key); return 'item'; }
  return moved ? 'choose' : '';
}

// The presses of the rows, as the table's own hits (a row stands for its whole width).
function hits(view) {
  return view.marks.flatMap(m => Array.from({ length: m.rows }, (_, i) => ({ row: m.at + i, left: 0, right: 4096, action: ['status', m.key] })));
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
// Nothing the answer carries is dropped here: its size was kept by the backend (an answer past the popup's limit has no list), and
// the text of an item the screen can open is as long as the document wrote it.
function entriesOf(result) {
  const styles = Array.isArray(result.styles) ? result.styles : [];
  if (!Array.isArray(result.list)) return null;
  const rowsIn = v => (Array.isArray(v) ? v : []).map(row => runParts(row, styles));
  return result.list.filter(e => e && typeof e === 'object').map(e => ({ rows: rowsIn(e.rows),
    ...(typeof e.key === 'string' && e.key ? { key: e.key } : {}), ...(typeof e.group === 'string' ? { group: e.group } : {}),
    ...(e.when === 'open' ? { when: 'open' } : {}), ...(Array.isArray(e.alt) ? { alt: rowsIn(e.alt) } : {}),
    ...(typeof e.text === 'string' && e.text ? { text: e.text } : {}), ...(Array.isArray(e.more) && e.more.length ? { more: rowsIn(e.more) } : {}),
    ...(e.lazy ? { lazy: true } : {}) }));
}

module.exports = { layout, neighbour, jump, press, hits, runParts, rowsOf, entriesOf, wrap };
