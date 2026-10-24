# Worker Type: Review

Inspect only the assigned question and evidence. Source is read-only unless the
route grants a narrow artifact-write scope. Record prioritized findings,
locations, checks, and uncertainty in the review artifact. Do not implement
fixes, widen the review, or dispatch another worker.

When the assigned question is spec-backed, read the current governing PRD and
register that read under your own guard identity before recording any
spec-relevant finding or verdict. The write gate only blocks a canonical write,
so nothing otherwise stops a review leg from judging a specification it never
read; that obligation is yours, not the gate's.
