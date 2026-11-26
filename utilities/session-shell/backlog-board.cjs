'use strict';
// The BACKLOG board (11-26, scheme A; the person: "A — FIX · TASK · NOTE에 상태(Discuss 등)와 영역을 더하고, 판은 영역별로 묶어 봄").
//
// The backend (`session_status.backlog_board`) sends the document's head and one short row per item, newest first (the latest date
// first, the same date in the document's order, no date last): `[key, kind, date, area, state, title, campaign]`. This file makes the
// status list's entries (`status-list.cjs`, which lays them out and moves the choice) from those rows by the board's own state: what
// they are grouped by (`k` kind, `a` area, `d` date), which state is shown (`t`: All, Discuss, Ready, Waiting, No state) and what is
// being found (`/` a title, `Tab` first for an area's name). That state is the open popup's alone (`newState`, kept on the popup's table);
// nothing is written, asked or started here, and nothing is read but the answer it is given.
//
// The groups' open state is the status list's own (`fold`, a set of the open groups' ids); a view that filters or finds stands with every
// group open (its heads are not pressed). Inside a group the items keep the answer's order, so the newest stand first.

const AXIS_WORDS = { kind: 'Kind', area: 'Area', date: 'Date' };
const AXIS_KEYS = { k: 'kind', a: 'area', d: 'date' };
const KIND_ORDER = ['fix', 'task', 'tbd', 'note'];
const KIND_WORDS = { fix: 'FIX', task: 'TASK', note: 'NOTE', tbd: 'TBD' };
const KIND_STYLES = { fix: 'lvl_y', task: 'lvl_g', note: 'dim', tbd: 'dim' };
const FILTERS = ['', 'discuss', 'ready', 'waiting', 'none'];
const FILTER_WORDS = { '': 'All', discuss: 'Discuss', ready: 'Ready', waiting: 'Waiting', none: 'No state' };
const STATES = { discuss: 'Discuss', ready: 'Ready', waiting: 'Waiting' };
const DAY = /^\d{4}-\d{2}-\d{2}$/;
const AREA_CELLS = 12;          // the area column is at most this wide (a longer name is cut with `…`)
const DETAIL_INDENT = 10;       // an opened item's lines stand under the date column: the slot, the group's two cells, the kind and two blanks
const FIND_MOST = 80;           // characters of what is being found

const defaultMeasure = text => [...String(text)].reduce((n, c) => n + (c.codePointAt(0) > 0x2e7f ? 2 : 1), 0);
// Case and runs of blanks are not told apart when finding (an area's own name is the backend's: the first one the document wrote).
const fold = text => String(text).split(/\s+/).filter(Boolean).join(' ').toLowerCase();

// The board from the backend's answer (`result.board`), or null where the answer has none. Every value is checked here: a row with no key is
// left out, a kind that is none of the three is TBD, a date that is not `YYYY-MM-DD` is none, a state that is none of the three is none.
function fromAnswer(result) {
  const b = result && result.board;
  if (!b || typeof b !== 'object' || !Array.isArray(b.items)) return null;
  const styles = Array.isArray(result.styles) ? result.styles : [];
  const named = {};
  for (const [name, at] of Object.entries(b.styles && typeof b.styles === 'object' ? b.styles : {})) if (Number.isInteger(at) && styles[at]) named[name] = styles[at];
  const campaigns = Array.isArray(b.campaigns) ? b.campaigns.map(String) : [];
  const items = [];
  for (const row of b.items) {
    if (!Array.isArray(row) || typeof row[0] !== 'string' || !row[0].startsWith('i:')) continue;
    const kind = ['fix', 'task', 'note'].includes(row[1]) ? row[1] : 'tbd';
    items.push({ key: row[0], kind, date: DAY.test(String(row[2] || '')) ? String(row[2]) : '', area: String(row[3] || '').trim(),
      state: STATES[row[4]] ? row[4] : '', title: String(row[5] ?? ''), campaign: campaigns[row[6]] ?? '' });
  }
  const count = v => Number.isInteger(v) && v >= 0 ? v : 0, c = b.counts && typeof b.counts === 'object' ? b.counts : {};
  return { title: String(b.title || ''), updated: String(b.updated || ''), old: b.old === true, cut: b.cut === true,
    total: Math.max(count(b.total), items.length), items, styles: named,
    counts: Object.fromEntries(['fix', 'task', 'note', 'tbd', 'discuss', 'ready', 'waiting'].map(k => [k, count(c[k])])) };
}

// The board's state: grouped by kind, every state shown, nothing found, nothing being typed.
function newState() { return { axis: 'kind', filter: '', find: null, typing: null, ready: false }; }
const narrowed = bs => !!(bs.filter || (bs.find && bs.find.text));

function visible(board, bs) {
  let rows = board.items;
  if (bs.filter) rows = rows.filter(r => bs.filter === 'none' ? !r.state : r.state === bs.filter);
  if (bs.find && bs.find.text) {
    const q = fold(bs.find.text);
    rows = rows.filter(r => fold(bs.find.field === 'area' ? r.area : r.title).includes(q));
  }
  return rows;
}

// The groups of `rows` by `axis`: kinds in the order FIX, TASK, TBD, NOTE; areas by name, `No area` last; dates newest first, `No date` last.
function groups(rows, axis) {
  const by = new Map(), value = r => axis === 'kind' ? r.kind : axis === 'area' ? r.area : r.date;
  for (const r of rows) { const v = value(r); if (!by.has(v)) by.set(v, []); by.get(v).push(r); }
  let names = [...by.keys()];
  if (axis === 'kind') names = KIND_ORDER.filter(k => by.has(k));
  else if (axis === 'area') names.sort((x, y) => (x === '') - (y === '') || x.localeCompare(y, 'en', { sensitivity: 'base' }) || (x < y ? -1 : x > y ? 1 : 0));
  else names.sort((x, y) => (x === '') - (y === '') || (x < y ? 1 : x > y ? -1 : 0));
  return names.map(v => ({ id: axis + ':' + (v ? '=' + v : '-'), word: axis === 'kind' ? KIND_WORDS[v] : v || (axis === 'area' ? 'No area' : 'No date'),
    style: axis === 'kind' ? KIND_STYLES[v] : v ? 'sect' : 'dim', items: by.get(v) }));
}

function painter(board) {
  return (text, style) => ({ text: String(text), ...(style ? board.styles[style] || (style === 'dim' ? { dim: true } : {}) : {}) });
}
function pad(text, cells, measure) {
  const used = measure(text);
  if (used <= cells) return text + ' '.repeat(cells - used);
  let out = '', n = 0;
  for (const ch of String(text)) { const w = measure(ch); if (n + w > cells - 1) break; out += ch; n += w; }
  return out + '…' + ' '.repeat(Math.max(0, cells - 1 - n));
}
function cutText(text, cells, measure) {
  if (measure(text) <= cells) return [text, false];
  let out = '', n = 0;
  for (const ch of String(text)) { const w = measure(ch); if (n + w > cells - 1) break; out += ch; n += w; }
  return [out + '…', true];
}

// Which columns stand at `inner` cells: kind always, then the state (where some item is in Discuss or Waiting), the date and the area; short of
// cells the area goes first, then the date, so the title keeps some room.
function columns(board, inner, measure) {
  const areaCells = Math.min(AREA_CELLS, Math.max(measure('No area'), ...board.items.map(r => measure(r.area || ''))));
  const cols = { date: true, area: true, state: board.items.some(r => r.state === 'discuss' || r.state === 'waiting'), areaCells };
  const lead = () => 4 + 4 + 2 + (cols.date ? 12 : 0) + (cols.area ? areaCells + 2 : 0) + (cols.state ? 9 : 0);
  if (inner - lead() < 24) cols.area = false;
  if (inner - lead() < 16) cols.date = false;
  return cols;
}

function itemRow(board, r, cols, inner, measure) {
  const S = painter(board);
  const parts = [S('  '), S('  '), S(pad(KIND_WORDS[r.kind], 4, measure), KIND_STYLES[r.kind]), S('  ')];
  if (cols.date) parts.push(S(pad(r.date || '—', 10, measure), 'dim'), S('  '));
  if (cols.area) parts.push(S(pad(r.area || 'No area', cols.areaCells, measure), 'dim'), S('  '));
  // (Discuss and Waiting before the title: what is to be talked over, what waits on something named; Ready and no state stand as blanks)
  if (cols.state) parts.push(r.state === 'discuss' ? { ...S('Discuss', 'loc_repo'), bold: true } : S(r.state === 'waiting' ? 'Waiting' : '       ', 'soft'), S('  '));
  const used = parts.reduce((n, p) => n + measure(p.text), 0);
  const [title, cut] = cutText(r.title, Math.max(1, inner - used), measure);
  parts.push(S(title, 'soft'));
  return { row: parts, cut };
}

function number(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ','); }

// The board's entries for the status list at `inner` cells: the head (title, `updated …`, the counts, what the board shows), the groups and
// their items, then `full document ›`. Items are `lazy` (their lines are asked for when opened) and carry their whole title where the row cut it.
function entries(board, bs, { width = 96, measure = defaultMeasure } = {}) {
  const S = painter(board), inner = Math.max(8, width), out = [];
  const one = parts => [cutParts(parts, inner, measure)];
  // (a head line of ` · ` pieces breaks between them where the board is narrow, so what is filtered or found is never cut off)
  const pieces = (words, style) => {
    const rows = [];
    for (const piece of String(words).split(' · ')) {
      const last = rows[rows.length - 1];
      if (last !== undefined && measure(last + ' · ' + piece) <= inner) rows[rows.length - 1] = last + ' · ' + piece; else rows.push(piece);
    }
    return rows.map(row => cutParts([S(row, style)], inner, measure));
  };
  const E = (rows, extra = {}) => ({ rows, ...extra });
  const c = board.counts, shown = visible(board, bs), narrow = narrowed(bs);
  if (board.title) out.push(E(one([S(board.title, 'sect')])));
  out.push(E(one([...(board.old ? [S('! ', 'lvl_y')] : []), S(board.updated, 'dim')])));
  out.push(E(pieces(`${number(board.total)}${board.cut ? '+' : ''} total · FIX ${number(c.fix)} · TASK ${number(c.task)} · TBD ${number(c.tbd)} · NOTE ${number(c.note)}`, 'dim')));
  // (the read limits: the file is longer than is read, or the answer holds the newest of them only)
  if (board.cut) out.push(E(pieces(`read ${number(board.total)} · more in backlog.md (the file is longer than is read)`, 'dim')));
  if (board.items.length < board.total) out.push(E(pieces(`showing the newest ${number(board.items.length)} of ${number(board.total)} · ${number(board.total - board.items.length)} more in backlog.md`, 'dim')));
  const axis = Object.entries(AXIS_WORDS).map(([k, w]) => k === bs.axis ? '[' + w + ']' : w).join(' ');
  const bits = ['group ' + axis, 'showing ' + number(shown.length) + (shown.length !== board.items.length ? ' of ' + number(board.items.length) : ''), 'Discuss ' + number(c.discuss)];
  if (bs.filter) bits.push('state ' + FILTER_WORDS[bs.filter]);
  if (bs.find && bs.find.text) bits.push('find ' + (bs.find.field === 'area' ? 'Area' : 'Title') + ' "' + bs.find.text + '"');
  out.push(E(pieces(bits.join(' · '), 'dim')));
  out.push(E([[]]));
  const cols = columns(board, inner, measure);
  for (const g of groups(shown, bs.axis)) {
    const head = mark => one([S('  '), S(mark + ' ', 'soft'), S(g.word, g.style), S(' ' + number(g.items.length), 'dim')]);
    out.push(narrow ? E(head('▾')) : E(head('›'), { key: 'g:' + g.id, group: g.id, alt: head('▾') }));
    for (const r of g.items) {
      const it = itemRow(board, r, cols, inner, measure);
      out.push(E([it.row], { key: r.key, lazy: true, ...(it.cut ? { text: r.title } : {}), ...(narrow ? {} : { group: g.id, when: 'open' }) }));
    }
  }
  if (!shown.length) out.push(E(one([S('  '), S('nothing matches · Esc clears', 'dim')])));
  out.push(E([[]]));
  out.push(E(one([S('  '), S('full document ›', 'soft')]), { key: 'full' }));
  return out;
}
function cutParts(parts, cells, measure) {
  const out = [];
  let used = 0;
  for (const p of parts) {
    const w = measure(p.text);
    if (used + w <= cells) { out.push(p); used += w; continue; }
    const [text] = cutText(p.text, cells - used, measure);
    if (cells - used > 0) out.push({ ...p, text });
    break;
  }
  return out;
}

// Every item of the view in the order it stands, with its group.
function ordered(board, bs) { return groups(visible(board, bs), bs.axis).flatMap(g => g.items.map(r => ({ r, g }))); }

// The first answer the open board has (and an answer read again): the board's first group opens, or, where the popup was opened standing on an
// item (a press on one of the panel's rows), the group that item is in. An answer read again keeps the board's state as it is.
function prepare(board, bs, t) {
  if (bs.ready) return;
  bs.ready = true;
  const list = ordered(board, bs), at = list.find(e => e.r.key === t.sel) || list[0];
  if (at) { t.fold.add(at.g.id); t.sel = at.r.key; }
}

function selectFirst(board, bs, t) {
  const list = ordered(board, bs);
  if (!list.some(e => e.r.key === t.sel)) t.sel = list.length ? list[0].r.key : '';
  if (!narrowed(bs) && list.length) { const at = list.find(e => e.r.key === t.sel); if (at) t.fold.add(at.g.id); }
  t.reveal = true;
}

// `k`, `a`, `d`: the board grouped by kind, area or date (a new grouping opens its first group and stands on its first item).
function setAxis(board, bs, t, axis) {
  if (!AXIS_WORDS[axis] || bs.axis === axis) return false;
  bs.axis = axis;
  t.fold = new Set(); t.items = new Set();
  const list = ordered(board, bs);
  t.sel = list.length ? list[0].r.key : '';
  if (list.length) t.fold.add(list[0].g.id);
  t.scroll = 0; t.reveal = true;
  return true;
}
// `t`: the next state shown (All, Discuss, Ready, Waiting, No state).
function cycleFilter(board, bs, t) {
  bs.filter = FILTERS[(FILTERS.indexOf(bs.filter) + 1) % FILTERS.length];
  t.items = new Set(); t.scroll = 0;
  selectFirst(board, bs, t);
  return true;
}
// Home / End: the first or the last item of the view (its group opens with it).
function ends(board, bs, t, last) {
  const list = ordered(board, bs), at = last ? list[list.length - 1] : list[0];
  if (!at) return false;
  if (!narrowed(bs)) t.fold.add(at.g.id);
  t.sel = at.r.key; t.reveal = true;
  return true;
}
// `/`: a title is being found (`Tab` first: an area's name).
function startFind(bs) { bs.typing = { field: 'title', text: '', decoder: new TextDecoder('utf-8') }; return true; }
function typing(bs) { return !!(bs && bs.typing); }
// A key while finding: `tab`, `enter`, `escape`, `backspace`, or the bytes of what was typed (as the host reads them, one byte a character).
function type(board, bs, t, key, bytes = '') {
  const f = bs.typing;
  if (!f) return false;
  if (key === 'tab') f.field = f.field === 'title' ? 'area' : 'title';
  else if (key === 'escape') bs.typing = null;
  else if (key === 'backspace') f.text = [...f.text].slice(0, -1).join('');
  else if (key === 'enter') {
    bs.typing = null;
    bs.find = f.text.trim() ? { field: f.field, text: f.text.trim() } : null;
    t.items = new Set(); t.scroll = 0;
    selectFirst(board, bs, t);
  } else if (bytes) {
    const text = f.decoder.decode(Buffer.from(bytes, 'latin1'), { stream: true }).replace(/[\x00-\x1f\x7f-\x9f]/g, '');
    f.text = [...(f.text + text)].slice(0, FIND_MOST).join('');
  }
  return true;
}
// Esc on the board: what is being typed goes, then the opened items close, then what was found, then the state shown; false when there is
// nothing left to undo (the popup then closes, as every popup's Esc does).
function back(board, bs, t) {
  if (bs.typing) { bs.typing = null; return true; }
  if (t.items && t.items.size) { t.items = new Set(); t.reveal = true; return true; }
  if (bs.find) { bs.find = null; selectFirst(board, bs, t); return true; }
  if (bs.filter) { bs.filter = ''; selectFirst(board, bs, t); return true; }
  return false;
}
// The row that stands above the keys while a title or an area is being found.
function inputRow(board, bs, inner, measure = defaultMeasure) {
  if (!bs || !bs.typing) return null;
  const S = painter(board), f = bs.typing;
  return cutParts([S('/ ', 'loc_repo'), S((f.field === 'title' ? 'Title' : 'Area') + ': ', 'dim'), S(f.text, 'loc_repo'), S('▏', 'loc_repo'),
    S('   Tab field · Enter find · Esc cancel', 'dim')], Math.max(1, inner), measure);
}

module.exports = { fromAnswer, newState, prepare, entries, visible, groups, setAxis, cycleFilter, ends, startFind, typing, type, back, inputRow,
  AXIS_KEYS, DETAIL_INDENT, KIND_WORDS, FILTERS };
