"""Pure whole-screen draw orchestration for immutable explorer inputs."""
from dataclasses import dataclass, replace

from .grid import CellGrid, Hit
from .output import clip
from .view_model import ExplorerViewModel
from .chrome import NAV_WIDTH
from . import chrome, history, models, pages, peers, resources, tables


def _scope_title(vm, state):
    scope = str(state.scope or "-")
    kind, _, ident = scope.partition(":")
    if kind == "repo":
        repo = next((item for item in vm.repos
                     if str(item.get("id", item.get("name", ""))) == ident), None)
        return tables.repo_rule_label((repo or {}).get("name") or ident) or state.view
    if kind == "session":
        session = next((item for item in vm.sessions if str(item.get("tag")) == ident), None)
        if session:
            head = tables.repo_rule_label(session.get("repo"))
            return f"{head + ' › ' if head else ''}[{ident}]"
    if kind == "route":
        route = next((item for item in vm.routes if str(item.get("id")) == ident), None)
        if route:
            parent = f"[{route['session_tag']}] › " if route.get("session_tag") else ""
            title = clip(str(route.get("title") or "·"), 32)
            head = tables.repo_rule_label(route.get("repo"))
            return f"{head + ' › ' if head else ''}{parent}{route.get('tree_label') or ident} · {title}"
    return state.view


@dataclass(frozen=True)
class CellFrame:
    cells: tuple
    hits: tuple
    cols: int
    rows: int
    title: str = "OVERVIEW"
    status: str = ""

    def text_rows(self):
        return tuple("".join(cell[0] for cell in row) for row in self.cells)


def draw(view_model, nav_state=None, cols=140, rows=45, beat=1):
    """Draw without I/O, clocks, mutable globals, or terminal dependencies."""
    vm = ExplorerViewModel.from_mapping(view_model)
    from .state import NavState
    state = nav_state if isinstance(nav_state, NavState) else NavState.from_value(nav_state)
    state = replace(state, title=_scope_title(vm, state))
    # An open usage block takes two rows from the pane: the view lays itself out on a pane two
    # rows shorter (its own scroll windows and "fits" tests see that), and lands two rows lower.
    pushed_rows = 2 if state.usage_open and rows - 2 > 7 else 0
    view_rows = rows - pushed_rows
    grid = CellGrid(cols, view_rows)
    chrome.draw_explorer(grid, vm, state)
    pane_x = NAV_WIDTH + 2
    spill_y = max(0, int(view_rows) - 5)
    for x in range(pane_x, grid.cols):
        grid.put(x, spill_y, "━", "line")
    spill_baseline = tuple(grid.cells[spill_y][pane_x:])
    grid.mark_moved()
    if grid.cols < 120:
        # A narrow screen keeps the last column free: whatever a view would spill into it is cut.
        grid.pane_clip = (pane_x - 1, grid.cols - 1)
    if state.scope != "-":
        pages.draw_scope(grid, vm, state)
    elif state.view == "OVERVIEW":
        tables.draw_overview(grid, vm, state)
    elif state.view == "ROUTES":
        tables.draw_routes(grid, vm, state)
    elif state.view == "QUESTIONS":
        tables.draw_questions(grid, vm, state)
    elif state.view == "HISTORY":
        history.draw_history(grid, vm, state)
    elif state.view == "PEERS":
        peers.draw_peers(grid, vm, state)
    elif state.view == "COMPUTE":
        resources.draw_compute(grid, vm, state)
    elif state.view == "MEMORY":
        resources.draw_memory(grid, vm, state)
    elif state.view == "MODELS":
        models.draw_models(grid, vm, state)
    elif state.view == "SYSTEM":
        resources.draw_system(grid, vm, state)
    grid.pane_clip = None
    # DESIGN=4 reserves row 2 as the blank spacer and starts the pane body below
    # the joined VIEWS / pane title band on row 3.
    pane_overflow = tuple(grid.cells[spill_y][pane_x:]) != spill_baseline
    for y in range(max(3, view_rows - 6), 2, -1):
        for x in range(pane_x - 1, grid.cols):
            grid.cells[y + 1][x] = grid.cells[y][x]
    for x in range(pane_x, grid.cols):
        grid.cells[3][x] = (" ", None, None)
    grid.hits = [Hit(hit.y + 1, hit.x0, hit.x1, hit.action)
                 if hit.y >= 3 and hit.x0 >= pane_x else hit for hit in grid.hits]
    grid.mark_moved()
    # View content owns rows above the shared feed/key/status bands only. Mark
    # overflow and discard offscreen cells/hits before those shared bands draw.
    body_end = max(0, int(rows) - 4)
    view_body_end = max(0, int(view_rows) - 4)
    pushed = False
    if pushed_rows:
        # What the view left in the last two rows of its short pane runs past the real pane's end.
        pane = NAV_WIDTH + 1
        pushed = any(cell[0] != " " or cell[1] is not None or cell[2] is not None
                     for row in grid.cells[view_body_end:view_body_end + 2] for cell in row[pane:])
    overflow = pane_overflow or pushed or any(any(cell[0] != " " or cell[1] is not None or cell[2] is not None
                       for cell in row) for row in grid.cells[view_body_end:])
    if pushed_rows and body_end > 3:
        pane = NAV_WIDTH + 1
        prior = grid
        grid = CellGrid(cols, rows)
        for row_index in range(5, body_end):
            source = row_index - pushed_rows
            if source < prior.rows:
                grid.cells[row_index][pane:] = prior.cells[source][pane:]
        grid.mark_moved()
        view_hits = [Hit(hit.y + pushed_rows, hit.x0, hit.x1, hit.action) if 3 <= hit.y else hit
                     for hit in prior.hits if hit.x0 >= pane or hit.y < 0]
        chrome.draw_explorer(grid, vm, state)
        grid.hits.extend(view_hits)
    grid.hits = [hit for hit in grid.hits if hit.y < body_end or hit.y == -1]
    for row_index in range(body_end + 1, grid.rows):
        grid.fill(0, grid.cols, row_index, " ")
    chrome.draw_chrome(grid, vm, state, beat)
    if overflow and body_end < grid.rows:
        # The reference uses the terminal bottom row for its clipped-pane cue.
        grid.mark_moved()
        grid.put(max(0, grid.cols - 18), body_end, " ↓ more below ", "dim", limit=grid.cols - 1)
    parent = "-"
    if state.scope.startswith("session:"):
        tag = state.scope.partition(":")[2]
        session = next((item for item in vm.sessions if str(item.get("tag")) == tag), None)
        repo = (session or {}).get("repo_id", (session or {}).get("repo"))
        if tables.repo_label(repo):
            parent = "repo:" + str(repo)
    elif state.scope.startswith("route:"):
        ident = state.scope.partition(":")[2]
        route = next((item for item in vm.routes if str(item.get("id")) == ident), None)
        route = route or {}
        parent = str(route.get("session_tag") or ("repo:" + str(route.get("repo")) if route.get("repo")
                                                  else "-"))
    grid.hits.append(Hit(-1, 0, 0, ("parent", parent)))
    grid.add_hit(1, int(cols) - 9, int(cols), ("usage",))
    return CellFrame(grid.snapshot(), tuple(grid.hits), int(cols), int(rows),
                     _scope_title(vm, state), state.status)

