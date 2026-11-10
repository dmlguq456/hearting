#!/usr/bin/env python3
"""Codex UserPromptSubmit bridge for portable prompt lifecycle signals."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
PREFLIGHT = ROOT / "adapters" / "codex" / "bin" / "preflight.sh"
RECALL_HOOK = ROOT / "hooks" / "mem-recall-inject.sh"
SESSION_TIDY = ROOT / "utilities" / "session_tidy.py"
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
UTILITIES = ROOT / "utilities"
if str(UTILITIES) not in sys.path:
    sys.path.insert(0, str(UTILITIES))

from fleet.token_accounting import record_accounting  # noqa: E402
from fleet.token_budget import DIRECTIVE_TEXTS  # noqa: E402
from session_summary_trigger import launch_trigger  # noqa: E402

SD111_SWEEP_RECIPIENT_KIND = "codex-stop-hook"
SD111_SWEEP_SESSION_GENERATION = "unsupported"


@dataclass
class PreflightResult:
    stdout: str = ""
    returncode: int = 0
    timed_out: bool = False


def first_string(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def nested_string(payload: dict[str, Any], *keys: str) -> str:
    direct = first_string(payload, *keys)
    if direct:
        return direct
    for key in ("context", "workspace", "session", "payload", "event", "input", "data"):
        value = payload.get(key)
        if isinstance(value, dict):
            found = nested_string(value, *keys)
            if found:
                return found
    return ""


def load_payload() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def cwd(payload: dict[str, Any]) -> str:
    return nested_string(payload, "cwd", "working_directory", "workingDirectory") or os.getcwd()


def session_id(payload: dict[str, Any]) -> str:
    sid = nested_string(payload, "session_id", "sessionID", "thread_id", "threadID")
    session = payload.get("session")
    if not sid and isinstance(session, dict):
        sid = first_string(session, "id")
    return sid or "codex-hook"


def interaction_session_id(payload: dict[str, Any]) -> str:
    sid = nested_string(payload, "session_id", "sessionID", "thread_id", "threadID")
    session = payload.get("session")
    if not sid and isinstance(session, dict):
        sid = first_string(session, "id")
    return sid


def turn_id(payload: dict[str, Any]) -> str:
    return nested_string(payload, "turn_id", "turnID", "message_id", "messageID")


def candidate_context(payload: dict[str, Any], current_cwd: str, sid: str) -> tuple[str, Any]:
    """Run the bounded D-55 capsule probe; every failure is zero context.

    The bridge renders more context after this probe, so the helper keeps its
    seen-ID/recall receipt pending and returns it here. The caller commits only
    after ``emit_context`` returned (the same boundary the native queue ack uses).
    """
    prompt = nested_string(payload, "prompt")
    if not prompt:
        return "", None
    command = [
        str(RECALL_HOOK), "--prompt", prompt, "--cwd", current_cwd,
        "--session-id", sid, "--runtime", "codex", "--format", "hook-json",
    ]
    current_turn = turn_id(payload)
    if current_turn:
        command += ["--turn-id", current_turn]
    env = os.environ.copy()
    # The installed hook owns this bridge. Do not let a stale/invalid inherited
    # AGENT_HOME redirect the prompt probe to another tree's memory launcher.
    env["AGENT_HOME"] = str(ROOT)
    env["MEM_CANDIDATE_DEFER_COMMIT"] = "1"
    try:
        result = subprocess.run(
            command, cwd=str(ROOT), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "", None
    if result.returncode != 0:
        return "", None
    try:
        value = json.loads(result.stdout)
    except (TypeError, ValueError):
        return "", None
    if not isinstance(value, dict):
        return "", None
    output = value.get("hookSpecificOutput")
    context = output.get("additionalContext") if isinstance(output, dict) else ""
    return (context if isinstance(context, str) else "",
            value.get("deferredCandidateReceipt"))


def card_context(payload: dict[str, Any], current_cwd: str, *, startup=None, human=None,
                 read_context=None) -> tuple[str, Any]:
    """Render through the shared hook, committing receipts after the outer output."""
    sid = interaction_session_id(payload)
    if not sid or not SESSION_TIDY.is_file():
        return "", None
    contexts, deferred = [], []
    try:
        import session_tidy
        session_tidy.run_hook(
            "codex", "prompt", sid, cwd=current_cwd,
            transcript=nested_string(payload, "transcript_path", "transcriptPath"),
            prompt_text=nested_string(payload, "prompt"), emit=contexts.append,
            emit_human=human.append if human is not None else None,
            startup_context=startup if startup is not None else {}, deferred=deferred,
            read_context=read_context)
    except Exception:
        return "", None
    return "\n".join(contexts), deferred


def run_preflight_result(*args: str, timeout_seconds: float | None = None,
                         env_extra: dict[str, str] | None = None) -> PreflightResult:
    env = os.environ.copy()
    env.setdefault("AGENT_HOME", str(ROOT))
    if env_extra:
        env.update(env_extra)
    if timeout_seconds is not None:
        process = subprocess.Popen(
            [str(PREFLIGHT), *args], cwd=str(ROOT), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except (OSError, ProcessLookupError):
                pass
            process.communicate()
            return PreflightResult(returncode=process.returncode or -9, timed_out=True)
        if stderr:
            sys.stderr.write(stderr)
        return PreflightResult(stdout=stdout, returncode=process.returncode)
    result = subprocess.run(
        [str(PREFLIGHT), *args],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.stderr:
        sys.stderr.write(result.stderr)
    return PreflightResult(stdout=result.stdout, returncode=result.returncode)


def run_preflight(*args: str, timeout_seconds: float | None = None) -> str:
    return run_preflight_result(*args, timeout_seconds=timeout_seconds).stdout


def is_worker_session() -> bool:
    return (
        os.environ.get("AGENT_SESSION_ROLE", "").lower() == "worker"
        or os.environ.get("AGENT_DISPATCH_CHILD") == "1"
        or bool(os.environ.get("AGENT_DISPATCH_DEPTH"))
        or bool(os.environ.get("OPENCODE_DISPATCH_SLUG"))
        or os.environ.get("FLEET_TITLE_REFRESH") == "1"
    )


def token_budget_timeout() -> float:
    try:
        value = float(os.environ.get("CODEX_TOKEN_BUDGET_HOOK_TIMEOUT_SECONDS", "1.0"))
    except ValueError:
        value = 1.0
    return min(5.0, max(0.05, value))


def emit_context(event_name: str, parts: list[str], *, system_message: str = "") -> None:
    context = "\n".join(part.strip() for part in parts if part.strip())
    output = {}
    if context:
        output["hookSpecificOutput"] = {"hookEventName": event_name, "additionalContext": context}
    if system_message.strip():
        output["systemMessage"] = system_message.strip()
    if not output:
        return
    print(json.dumps(output, ensure_ascii=False), flush=True)


def load_token_receipt(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > 2048:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("receipt_version") != 1:
        return None
    if value.get("outcome") not in {"zero", "emission"}:
        return None
    sample = value.get("session_total_tokens")
    if sample is not None and (not isinstance(sample, int) or isinstance(sample, bool) or sample < 0):
        return None
    return value


def token_budget_context(current_cwd: str, sid: str) -> str:
    """Run Phase 1 output and record one Phase 2 outcome without changing bytes."""

    temporary: tempfile.TemporaryDirectory[str] | None = None
    receipt_path: Path | None = None
    contribution = ""
    event: dict[str, Any] = {
        "outcome": "zero", "zero_reason": "timeout_or_error",
        "directive_id": None, "directive_utf8_bytes": 0,
        "session_total_tokens": None,
    }
    try:
        try:
            temporary = tempfile.TemporaryDirectory(prefix="codex-token-budget-")
            receipt_path = Path(temporary.name) / "result.json"
        except OSError:
            # Receipt infrastructure is optional and must not change Phase 1 output.
            temporary = None
            receipt_path = None
        env_extra = (
            {"AGENT_TOKEN_BUDGET_RESULT_PATH": str(receipt_path)}
            if receipt_path is not None else None
        )
        try:
            result = run_preflight_result(
                "token-budget", current_cwd, sid, "hook",
                timeout_seconds=token_budget_timeout(), env_extra=env_extra,
            )
        except (OSError, ValueError, TypeError):
            result = PreflightResult(returncode=-1)
        receipt = (
            load_token_receipt(receipt_path)
            if receipt_path is not None and result.returncode == 0 and not result.timed_out
            else None
        )
        delivered = next(
            ((directive_id, text) for directive_id, text in DIRECTIVE_TEXTS.items()
             if result.stdout == text + "\n"),
            None,
        ) if result.returncode == 0 and not result.timed_out else None
        if delivered is not None:
            directive_id, contribution = delivered
            expected_bytes = len(contribution.encode("utf-8"))
            if (receipt is not None and receipt.get("outcome") == "emission"
                    and receipt.get("directive_id") == directive_id
                    and receipt.get("directive_utf8_bytes") == expected_bytes):
                event = receipt
            else:
                event = {
                    "outcome": "emission", "zero_reason": None,
                    "directive_id": directive_id,
                    "directive_utf8_bytes": expected_bytes,
                    "session_total_tokens": None,
                }
        elif (result.returncode == 0 and not result.timed_out and result.stdout == ""
              and receipt is not None and receipt.get("outcome") == "zero"
              and receipt.get("zero_reason") in {
                  "normal", "unknown", "native", "same_band", "degraded"}
              and receipt.get("directive_id") is None
              and receipt.get("directive_utf8_bytes") == 0):
            event = receipt
    finally:
        try:
            record_accounting(sid, event, adapter="codex")
        except Exception:
            # Accounting is an observational side effect and never owns output.
            pass
        if temporary is not None:
            try:
                temporary.cleanup()
            except OSError:
                pass
    return contribution


def _sd111_sweep_marker(root: Path, sid: str) -> Path:
    digest = hashlib.sha256(sid.encode("utf-8")).hexdigest()
    return root / "session-sweep-markers" / f"{digest}.marker"


def _sd111_inbox_delivery_ids(root: Path, sid: str) -> list[str] | None:
    """The current derived inbox's own records, or ``None`` for a full first scan.

    Only ids stored under this session's own key can be claimed by this
    generation-proof sweep; a predecessor record stays with its carrier.
    """
    try:
        import dispatch_recipient_inbox as inbox
        dispatch_root = inbox.dispatch_inbox_root(jobs=root / "jobs.log")
        payload = inbox.read_inbox(dispatch_root, "codex", sid)
        if payload is None or not inbox.is_current(payload, dispatch_root):
            return None
        return [item["delivery_id"] for item in payload["delivery_ids"]
                if item.get("storage_key") == sid]
    except Exception:  # noqa: BLE001 - discovery only; the own-dir scan remains
        return None


def sd111_first_prompt_sweep(sid: str) -> None:
    """SD-111 P4 -- Codex carrier 2. There is no confirmed Codex
    SessionStart-equivalent surface (plan §P4), so this runs at the first
    UserPromptSubmit of a session only -- gated by a durable per-session
    marker file, never on every turn. Codex is measured `unproven` for
    session-generation proof (§3.5), so the sweep is always refused with
    `pending-delivery-generation-unproven`; that refusal is the point.
    Fail-open end to end: any failure here is swallowed and never surfaces to
    the prompt.
    """

    if not sid:
        return
    try:
        from dispatch_contract import dispatch_state_roots, resolve_agent_home
        from dispatch_session_sweep import sweep
    except Exception:
        return
    try:
        roots = dispatch_state_roots(resolve_agent_home())
    except Exception:
        return
    if not roots:
        return
    marker = _sd111_sweep_marker(roots[0], sid)
    if marker.exists():
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
    except OSError:
        return
    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        try:
            delivery_ids = _sd111_inbox_delivery_ids(root, sid)
            sweep(root, SD111_SWEEP_RECIPIENT_KIND, sid, SD111_SWEEP_SESSION_GENERATION,
                  delivery_ids=delivery_ids)
        except Exception:
            continue


def native_queue_prompt_receipts(sid: str, *, read_context=None):
    """Render this exact session's pending queue receipts on a real prompt."""
    if not sid:
        return [], []
    from dispatch_contract import dispatch_state_roots, resolve_agent_home
    from dispatch_session_sweep import delivery_context, sweep_deliver
    from dispatch_notice_state import NoticeReadContext
    notice_context = NoticeReadContext()
    batches = []
    for root in dict.fromkeys(dispatch_state_roots(resolve_agent_home())):
        records, _ = sweep_deliver(root, "codex-native-queue", sid, notice_context=notice_context,
                                  read_context=read_context)
        if records:
            batches.append((root, records))
    context = delivery_context(batches)
    return batches, [context] if context else []


def fit_prompt_receipts(sid: str, batches, other_parts: list[str], *, rendered=()):
    """The receipts text that fits, whole, beside the rest of this hook's context.

    Codex shows one hook context of at most about 2,500 tokens inline and
    elides the middle of a longer one (PG-47), so only the leading records
    whose text fits in what the other parts leave are rendered and ACKed; the
    rest are handed back pending for the next boundary. Returns
    ``(text, kept_batches)``; ``rendered`` is the whole text already rendered.
    """
    try:
        from dispatch_session_sweep import (CODEX_CONTEXT_BYTES, fit_inline, release_claims,
                                            utf8_len)
        others = "\n".join(part.strip() for part in other_parts if part.strip())
        # Only what the other parts leave; no floor, so a full context holds
        # every receipt back pending rather than push the whole past the limit.
        budget = CODEX_CONTEXT_BYTES - utf8_len(others) - 1
        whole = "\n".join(part.strip() for part in rendered if part.strip())
        if whole and utf8_len(whole) <= budget:
            return whole, batches
        if budget <= 0:
            release_claims(sid, batches, "codex-native-queue",
                           reason="codex-prompt-context-over-limit")
            return "", []
        text, kept, rest = fit_inline(batches, budget, measure=utf8_len)
        if rest:
            release_claims(sid, rest, "codex-native-queue", reason="codex-prompt-context-over-limit")
        return text, kept
    except Exception:
        # Nothing shown is ACKed; the claims return at their lease.
        return "", []


def native_receiving_turn(payload: dict[str, Any], thread_id: str) -> dict[str, str] | None:
    """Bind the hook to a turn ID that Codex itself exposes and confirms.

    ``message_id`` aliases are deliberately excluded: they do not prove a
    native turn binding.
    """
    turn_id = nested_string(payload, "turn_id", "turnID")
    if not thread_id or not turn_id:
        return None
    try:
        from codex_queue_dispatch import resolve_queue_socket
        import codex_queue_delivery
        turn = codex_queue_delivery.read_exact_turn(resolve_queue_socket(), thread_id, turn_id)
    except (OSError, ValueError, RuntimeError):
        return None
    if (turn is None or turn.get("id") != turn_id
            or turn.get("status") not in {"inProgress", "in_progress", "running"}):
        return None
    return {"thread_id": thread_id, "turn_id": turn_id, "status": turn["status"]}


def peer_notice(payload: dict[str, Any], current_prompt: str, current_cwd: str) -> None:
    """F-100c — the receive side of a herdr steer, harness-neutral: when the prompt
    carries the `(peer-from: <harness> <sid> <name>)` trailer a steward appended, write
    one `notice` peer_message_v1 under THIS session's exact id (the same record the
    Claude hook and the OpenCode plugin write). Fail-soft, bounded, never prints."""
    if not current_prompt or ("peer-from:" not in current_prompt
                              and "runtime-from:" not in current_prompt):
        return
    sid = interaction_session_id(payload)
    if not sid:
        return
    try:
        import importlib.util
        tool = ROOT / "utilities" / "peer-message.py"
        spec = importlib.util.spec_from_file_location("_peer_message", str(tool))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.receive_peer_message(current_prompt, {"harness": "codex", "session_id": sid},
                                 os.path.basename(current_cwd.rstrip("/")) if current_cwd else "",
                                 summary_text="herdr steer received")

    except Exception:
        return


def retry_pending_peer(sid: str) -> None:
    if not sid:
        return
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_pending_peer", ROOT / "utilities" / "peer-message.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # Bounded like the Claude prompt hook: unreached rows stay pending.
        mod.retry_pending_codex({"harness": "codex", "session_id": sid},
                                deadline=time.monotonic() + 2.5)
    except Exception:
        pass


def prompt_probe_context(payload, current_cwd, sid, *, startup_due):
    """Overlap independent reads, keeping their existing output/receipt order."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as probes:
        evidence = (probes.submit(run_preflight, "local-evidence", current_cwd)
                    if startup_due else None)
        candidate = probes.submit(candidate_context, payload, current_cwd, sid)
        briefing = probes.submit(run_preflight, "briefing", current_cwd)
        budget = probes.submit(token_budget_context, current_cwd, sid)
        candidate_text, candidate_deferred = candidate.result()
        return ([evidence.result() if evidence is not None else "", candidate_text,
                 briefing.result(), budget.result()], candidate_deferred)


def main() -> int:
    payload = load_payload()
    # Dispatch prompts carry their own explicit status/prompt-signal/mode
    # bootstrap. Main-only briefing and token context would be duplicate
    # context and can recursively create model work (D-42).
    if is_worker_session():
        try:
            sys.path.insert(0, str(ROOT / "utilities"))
            from peer_obligations import request_obligation_recovery
            request_obligation_recovery(lifecycle_root=ROOT)
        except Exception:
            pass
        return 0
    context_notice = ""
    try:
        from context_alert import codex_notice
        event_name = nested_string(payload, "hook_event_name", "event_name", "event")
        normalized = {
            "hook_event_name": event_name,
            "session_id": interaction_session_id(payload),
            "transcript_path": nested_string(payload, "transcript_path", "transcriptPath"),
        }
        context_notice = codex_notice(normalized)
    except Exception:
        # The notice is advisory; ordinary prompt lifecycle remains available.
        context_notice = ""
    current_cwd = cwd(payload)
    interaction_sid = interaction_session_id(payload)
    try:
        # Baseline for the automatic change record; detached, before any context work.
        from turn_change_record import launch as launch_turn_record
        launch_turn_record("start", "codex", payload)
    except Exception:
        pass
    try:
        # The daemon keeps this thread after its TUI exits; clear the wait on that exit.
        from codex_tui_exit import launch as watch_tui_exit
        watch_tui_exit(interaction_sid or "")
    except Exception:
        pass
    try:
        from herdr_session_projection import project
        project(payload, interaction_sid or "", worker=False)
    except Exception:
        pass
    if interaction_sid:
        try:
            from fleet import interaction

            interaction.clear_wait(interaction_sid, "codex")
        except Exception:
            pass
    sid = session_id(payload)
    current_prompt = nested_string(payload, "prompt")
    peer_notice(payload, current_prompt, current_cwd)
    retry_pending_peer(interaction_sid)
    if interaction_sid:
        launch_trigger(
            "codex", interaction_sid, "initial",
            transcript=nested_string(payload, "transcript_path", "transcriptPath"),
            anchor_text=current_prompt
        )
        # Codex runs no Stop hook when a turn fails; this turn's own observer
        # continues it after a transient model-capacity failure.
        # The observer already requires this exact native turn field. Avoid
        # loading its supervisor graph for payloads on which launch is a no-op.
        if first_string(payload, "turn_id"):
            try:
                from codex_capacity_resume import launch as launch_capacity_resume
                launch_capacity_resume(payload)
            except Exception:
                pass
    sd111_first_prompt_sweep(sid)

    # The card hook runs first: it is where a cleared window's successor is recorded, and the
    # receipts a predecessor left for it are swept only once that is known.
    startup = {"due": True}  # a failed card lookup must not suppress the evidence probe
    human_notices = []
    from peer_read_context import PeerReadContext
    discovery_context = PeerReadContext(compact_lineage=True)
    card, card_deferred = card_context(payload, current_cwd, startup=startup, human=human_notices,
                                       read_context=discovery_context)
    batches = []
    ackable = set()
    try:
        batches, receipt_parts = native_queue_prompt_receipts(sid, read_context=discovery_context)
        from dispatch_session_sweep import retain_receiving_turn
        ackable = retain_receiving_turn(
            batches, interaction_sid,
            native_receiving_turn(payload, interaction_sid),
            request_context=current_prompt,
        )
    except Exception:
        receipt_parts = []
    parts = []
    if ("peer-from:" in current_prompt or "runtime-from:" in current_prompt
            or "cross-session-message" in current_prompt):
        import importlib.util
        spec = importlib.util.spec_from_file_location("peer_receiver_guidance", ROOT / "utilities/peer-message.py")
        peer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(peer)
        parts.append(peer.receiver_guidance(current_prompt,
            {"harness": "codex", "session_id": interaction_sid}, cwd=current_cwd))
    parts.append(card)
    probe_parts, candidate_deferred = prompt_probe_context(
        payload, current_cwd, sid, startup_due=startup.get("due"))
    parts.extend(probe_parts)
    if context_notice:
        parts.append(context_notice)
    if batches or receipt_parts:
        receipts, batches = fit_prompt_receipts(sid, batches, parts, rendered=receipt_parts)
        if receipts:
            parts.insert(0, receipts)
    emit_context("UserPromptSubmit", parts,
                 system_message="\n".join(human_notices + ([context_notice] if context_notice else [])))
    # Commit candidate/card consumption only after the context above actually
    # rendered, and before the existing native-queue ack does the same for its
    # records. A timeout or render error before this point leaves the same
    # candidates and card generation due for the next prompt.
    if card_deferred:
        try:
            import session_tidy
            session_tidy.apply_deferred(card_deferred)
        except Exception:
            pass
    if candidate_deferred:
        try:
            memory_dir = ROOT / "tools" / "memory"
            if str(memory_dir) not in sys.path:
                sys.path.append(str(memory_dir))
            import mem
            mem.apply_candidate_receipt(candidate_deferred)
        except Exception:
            pass
    # Commit acknowledgement only after successful synchronous hook rendering.
    from dispatch_session_sweep import ack_delivered
    received = []
    for root, records in batches:
        retained = [record for record in records
                    if (str(root), record.get("delivery_id")) in ackable]
        if retained:
            ack_delivered(root, interaction_sid, retained,
                          acked_by="codex-native-prompt:" + interaction_sid)
            received.extend(retained)
    note_turn_received(interaction_sid, first_string(payload, "turn_id"), received, current_prompt)
    return 0


def note_turn_received(sid: str, native_turn: str, records: list, prompt: str) -> None:
    """Tell the in-turn carrier what this turn received, for its one Stop follow-through."""
    if not sid or not native_turn:
        return
    try:
        import codex_turn_carrier as carrier
        items = carrier.owed_items(records)
        peer = carrier.peer_report_item(prompt, sid)
        if peer:
            items.append(peer)
        carrier.note_received(sid, native_turn, items,
                              rendered=[str(record.get("delivery_id")) for record in records])
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
