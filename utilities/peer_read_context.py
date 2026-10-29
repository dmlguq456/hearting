#!/usr/bin/env python3
"""Per-scan reuse of immutable registry and handover read inputs.

Instances are short lived and owned by one peer-message scan. They cache no
delivery state, claim, acknowledgement, or authority across a state-changing
operation.
"""

from __future__ import annotations

from pathlib import Path


class PeerReadContext:
    def __init__(self, *, registry_append_only: bool = False):
        self.registry_append_only = registry_append_only
        self._registry_views = {}
        self._handover_snapshots = None
        self._handover_ledgers = {}
        self._lineage_ledgers = None
        self._lineage_graphs = {}
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
        key = str(jobs.resolve(strict=False))
        if key not in self._registry_views:
            try:
                self._registry_views[key] = registry_view(
                    jobs, append_only=(self.registry_append_only if append_only is None else append_only))
            except FileNotFoundError:
                self._registry_views[key] = None
        return self._registry_views[key]

    def handover_snapshots(self, loader):
        if self._handover_snapshots is None:
            self._handover_snapshots = tuple(loader())
        return self._handover_snapshots

    def handover_rows(self, seat, loader):
        key = (str(getattr(seat, "kind", "")), str(getattr(seat, "key", "")))
        if key not in self._handover_ledgers:
            self._handover_ledgers[key] = tuple(loader())
        return self._handover_ledgers[key]

    def lineage_ledgers(self, loader):
        """Read each raw seat ledger once for this short-lived lineage pass."""
        if self._lineage_ledgers is None:
            self._lineage_ledgers = loader()
        return self._lineage_ledgers

    def lineage_graph(self, signature, loader):
        """Reuse this read-only pass's graph; normal observations replace it.

        The signature detects writer changes inside the pass. Current authority
        at an effect boundary comes from a fresh readable lineage observation,
        even when that boundary was preceded by this same context's discovery.
        """
        key = tuple(signature)
        if key not in self._lineage_graphs:
            self._lineage_graphs = {key: loader()}
            self._lineage_summary = None
        return self._lineage_graphs[key]

    def lineage_summary(self, graph, loader):
        """Derived history index belongs to the exact readable graph, only for this pass."""
        if self._lineage_summary is None or self._lineage_summary[0] is not graph:
            self._lineage_summary = (graph, loader())
        return self._lineage_summary[1]

    def invalidate_lineage_graph(self):
        """An unavailable current dependency cannot leave authority cached."""
        self._lineage_graphs.clear()
        self._lineage_summary = None
