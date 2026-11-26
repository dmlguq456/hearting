"""End-to-end latency measurement helpers (not part of the shipped runtime).

``tools/measure_e2e_latency.py`` is the command; these modules build the
operating-size fixture, inject events with the real writers, observe the
producer and Fleet passes, and assemble the numbers. Nothing here runs in CI:
only ``tools/measure_e2e_latency.test.py`` (no measurement, no timing
assertion) is a regression test.
"""
