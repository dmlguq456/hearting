#!/usr/bin/env python3
"""Per-scan reuse of immutable registry and handover read inputs.

Instances are short lived and owned by one peer-message scan. They cache no
delivery state, claim, acknowledgement, or authority across a state-changing
operation.

Lifetime is one discovery pass plus one context per post-claim effect group: a
claim is an effect boundary, so the group that hands records over observes
current lineage again instead of inheriting the discovery pass's view. The
pre-claim ``currently_addressed`` check belongs to the discovery context, so
one complete registry snapshot per jobs path is memoized for that same
lifetime and is never shared across a claim.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

_UNOBSERVED = object()


class PeerReadContext:
    def __init__(self, *, registry_append_only: bool = False, compact_lineage: bool = False):
        self.registry_append_only = registry_append_only
        # Opt-in: read the runner-published compact relation rows instead of every
        # ledger byte. Only the effect path sets this; history readers
        # (``session_tidy.seat_lineage``) need every row and pass a plain context.
        self.compact_lineage = compact_lineage
        self._registry_views = {}
        self._registry_snapshots = {}
        self._jobs_keys = {}
        self._handover_snapshots = None
        self._handover_ledgers = {}
        self._lineage_graphs = {}
        self._lineage_hot = None     # (the signature object the held graph was built for, that graph)
        self._lineage_pin = None
        self._lineage_summary = None
        self._recipient_censuses = {}
        self._transfer_roots = None
        self._peer_notice_records = None

    def transfer_roots(self, loader):
        """Reuse candidate ledger roots for exact per-message transfer reads.

        The root list only narrows where each transfer record is read. It does
        not cache transfer contents or any delivery/recipient authority.
        """
        if self._transfer_roots is None:
            roots, _source = loader()
            self._transfer_roots = tuple(Path(root) for root in roots)
        return self._transfer_roots

    def peer_notice_records(self, loader):
        """Reuse the append-only peer receipt scan within this runner pass."""
        if self._peer_notice_records is None:
            self._peer_notice_records = tuple(loader())
        return self._peer_notice_records

    def recipient_census(self, endpoint, loader):
        """Reuse one endpoint's recipient listing within a single read pass."""
        key = str(endpoint or "default")
        if key not in self._recipient_censuses:
            self._recipient_censuses[key] = loader()
        return self._recipient_censuses[key]

    def current_attempt_row(self, jobs: Path, attempt_id: str, *, append_only=None):
        """Return the latest exact attempt row from this scan's registry view."""
        from dispatch_completion_join import ChildRow, JoinContractError

        jobs = Path(jobs)
        try:
            view = self.registry_view(jobs, append_only=append_only)
        except OSError as exc:
            raise JoinContractError("registry-unreadable") from exc
        if view is None:
            return None
        record, error = view.completion_attempt(attempt_id)
        if error:
            raise JoinContractError(error)
        if record is None:
            return None
        return ChildRow(record.order, record.status, record.slug, attempt_id,
                        record.raw, record.completion_metadata())

    def registry_view(self, jobs: Path, *, append_only=None):
        """Reuse one generation-checked registry view during this runner pass."""
        from dispatch_registry_cache import registry_view

        jobs = Path(jobs)
        key = self.jobs_key(jobs)
        if key not in self._registry_views:
            try:
                self._registry_views[key] = registry_view(
                    jobs, append_only=(self.registry_append_only if append_only is None else append_only))
            except FileNotFoundError:
                self._registry_views[key] = None
        return self._registry_views[key]

    def registry_snapshot(self, jobs: Path):
        """Reuse one complete registry generation (view and exact text) per pass.

        The snapshot carries no authority of its own: it is the observation the
        pre-claim ``currently_addressed`` check and the member's recipient
        projection read. A claim creates a new context, so the handed-over
        group never inherits the discovery generation.
        """
        from dispatch_registry_cache import registry_snapshot

        jobs = Path(jobs)
        key = self.jobs_key(jobs)
        if key not in self._registry_snapshots:
            try:
                self._registry_snapshots[key] = registry_snapshot(jobs)
            except FileNotFoundError:
                self._registry_snapshots[key] = None
        return self._registry_snapshots[key]

    def jobs_key(self, jobs) -> str:
        """A registry path resolved once per pass, not once per row.

        A scan resolves the same jobs path for every row it judges; each
        resolution lstats every path component (~1,600 rows per Bash call).
        """
        raw = str(jobs)
        if raw not in self._jobs_keys:
            self._jobs_keys[raw] = str(Path(jobs).resolve(strict=False))
        return self._jobs_keys[raw]

    def handover_snapshots(self, loader):
        if self._handover_snapshots is None:
            self._handover_snapshots = tuple(loader())
        return self._handover_snapshots

    def handover_rows(self, seat, loader):
        key = (str(getattr(seat, "kind", "")), str(getattr(seat, "key", "")))
        if key not in self._handover_ledgers:
            self._handover_ledgers[key] = tuple(loader())
        return self._handover_ledgers[key]

    def lineage_signature(self, observe):
        """Observe the lineage directory; inside ``pinned_lineage`` only once."""
        if self._lineage_pin is None:
            return observe()
        if self._lineage_pin is _UNOBSERVED:
            self._lineage_pin = observe()
        return self._lineage_pin

    @contextmanager
    def pinned_lineage(self):
        """Resolve one loop of recipients against a single lineage observation.

        Re-stating every ledger for each of a sweep's ~1,700 recipients made a
        10s prompt hook time out (2026-10-30). Outside the loop each read still
        observes writer changes, and effect boundaries read fresh lineage.
        """
        if self._lineage_pin is not None:
            yield
            return
        self._lineage_pin = _UNOBSERVED
        try:
            yield
        finally:
            self._lineage_pin = None

    def lineage_graph(self, signature, loader):
        """Reuse this read-only pass's graph; normal observations replace it.

        The signature detects writer changes inside the pass. Current authority
        at an effect boundary comes from a fresh readable lineage observation,
        even when that boundary was preceded by this same context's discovery.
        """
        hot = self._lineage_hot
        if hot is not None and hot[0] is signature and self._lineage_graphs:
            # The very signature object (an immutable tuple) the held graph was built for: no need
            # to hash its every ledger entry again for each of a sweep's thousands of lookups.
            return hot[1]
        key = tuple(signature)
        if key not in self._lineage_graphs:
            self._lineage_graphs = {key: loader()}
            self._lineage_summary = None
        graph = self._lineage_graphs[key]
        self._lineage_hot = (signature, graph)
        return graph

    def lineage_summary(self, graph, loader):
        """Derived history index belongs to the exact readable graph, only for this pass."""
        if self._lineage_summary is None or self._lineage_summary[0] is not graph:
            self._lineage_summary = (graph, loader())
        return self._lineage_summary[1]

    def replace_lineage_graph(self, signature, graph):
        """A fresh normal observation replaces this pass's graph.

        Inside ``pinned_lineage`` the pin moves to that same observation, so
        the rest of the loop reuses it instead of re-stating every ledger.
        """
        self.invalidate_lineage_graph()
        self.lineage_graph(signature, lambda: graph)
        if self._lineage_pin is not None:
            self._lineage_pin = tuple(signature)

    def invalidate_lineage_graph(self):
        """An unavailable current dependency cannot leave authority cached."""
        self._lineage_graphs.clear()
        self._lineage_hot = None
        if self._lineage_pin is not None:
            self._lineage_pin = _UNOBSERVED
        self._lineage_summary = None
