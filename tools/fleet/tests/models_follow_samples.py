"""Made-up answers of ``model_follow.fleet_view()`` for the MODELS view's tests and pictures.

Each sample is what the real function returns for one day of a made-up codex table (a list with one
item per harness, in the function's own words). Nothing here reads a table, a list or a record.
"""

TODAY = "2026-11-22"


def cell(target, model, effort, *, state="follow", why="default", since=None, held=None, following=None,
         lines=(), followable=True, reason=None):
    return {"target": target, "model": model, "effort": effort, "state": state, "why": why,
            "since": since, "held": held, "next": following, "lines": list(lines),
            "followable": followable, "reason": reason}


def followed(target, before, model, effort, date=TODAY, effort_was=None, moved=()):
    """A cell that followed: ``effort_was`` is the effort the follow lowered, ``moved`` the keys of the
    companion lines it moved (both in the answer's own words)."""
    return cell(target, model, effort, why="followed",
                since={"date": date, "from": before, "to": model, "effort_was": effort_was, "lines": list(moved)})


def unfollowable(target, model, effort, reason="not-in-list"):
    """A cell no update can follow (a Claude alias, a model its list does not know)."""
    return cell(target, model, effort, followable=False, reason=reason)


def kept(target, model, effort, date="2026-11-20"):
    return cell(target, model, effort, state="keep", why="kept",
                since={"date": date, "from": None, "to": None, "effort_was": None, "lines": []})


def undone(target, model, effort, later, date="2026-11-22"):
    """A cell the user put back to the value before its follow (a keep)."""
    return cell(target, model, effort, state="keep", why="reverted",
                since={"date": date, "from": model, "to": later, "effort_was": None, "lines": []})


def held(target, model, effort, refused, date="2026-11-22"):
    return cell(target, model, effort, state="held", why="held", held=refused,
                since={"date": date, "from": model, "to": refused, "effort_was": None, "lines": []})


def upcoming(model, effort, branch="will-follow", reason="newer"):
    return {"model": model, "effort": effort, "branch": branch, "reason": reason,
            "command": None, "fit_from": None}


def item(harness, cells=(), cli=None, status="ok", read_only=False):
    return {"harness": harness, "status": status, "reason": "", "cells": list(cells), "plan": [],
            "cli": cli, "states": {"followed": 0, "kept": 0, "held": 0}, "read_only": read_only}


def plain_codex():
    return [cell("profile/top", "gpt-6-astra", "xhigh"), cell("profile/deep", "gpt-6-astra", "high"),
            cell("profile/balanced-deep", "gpt-6.1-sol", "xhigh"),
            cell("profile/balanced", "gpt-6.1-terra", "xhigh"), cell("profile/light", "gpt-6-luna", "xhigh"),
            cell("profile/mini", "gpt-6-luna", "high")]


def plain_opencode():
    return [cell("profile/top", "opencode-go/glm-5.3-flash", "max"),
            cell("profile/deep", "opencode-go/glm-5.3-flash", "max"),
            cell("profile/balanced", "opencode-go/deepseek-v4.1-flash", "max"),
            cell("profile/light", "opencode-go/muse-spark", "max"),
            cell("tier/deep", "opencode-go/glm-5.3-flash", "max")]


CLI_PREVIOUS = {"harness": "codex", "version": "0.163.0", "kind": "previous-generation",
                "models": ["gpt-6.1-sol", "gpt-6-astra"], "command": None}


CLI_ANOTHER = {"harness": "codex", "version": "0.171.0", "kind": "list-from-another-cli",
               "list_version": "0.163.0", "models": [], "command": None}


def another_list(answer, cli=CLI_ANOTHER):
    """The same answer on a day the codex list was written by another CLI version: its cells are
    unknown (`followable` null, no next update) and the CLI says why."""
    import copy
    out = copy.deepcopy(answer)
    for entry in out:
        if entry["harness"] == "codex":
            entry["cli"] = copy.deepcopy(cli)
            for found in entry["cells"]:
                found.update(followable=None, reason="list-from-another-cli", next=None)
    return out


def samples():
    out = {}
    out["quiet"] = [item("claude"), item("codex", plain_codex()), item("opencode", plain_opencode())]
    out["followed"] = [
        item("claude"),
        item("codex", [followed("profile/top", "gpt-6-astra", "gpt-6.2-astra", "xhigh"),
                       followed("profile/deep", "gpt-6-astra", "gpt-6.2-astra", "high"),
                       followed("profile/balanced-deep", "gpt-6.1-sol", "gpt-6.2-sol", "xhigh"),
                       cell("profile/balanced", "gpt-6.1-terra", "xhigh"),
                       cell("profile/light", "gpt-6-luna", "xhigh"),
                       kept("profile/mini", "gpt-6-luna", "high"),
                       followed("tier/deep", "gpt-6.1-sol", "gpt-6.2-sol", "high"),
                       followed("tier/light", "gpt-6-luna", "gpt-6.1-luna", "medium")]),
        item("opencode", plain_opencode())]
    out["another-list"] = another_list(out["followed"])
    # the same table, the last follow dated three and four days before TODAY
    out["followed-3d"] = _redate(out["followed"], "2026-11-19")
    out["followed-4d"] = _redate(out["followed"], "2026-11-18")
    out["keep"] = [item("claude"), item("codex", [
        kept("profile/mini", "gpt-6-luna", "high"),
        undone("profile/light", "gpt-6-luna", "xhigh", "gpt-6.1-luna"),
        cell("profile/deep", "gpt-6-astra", "high")]), item("opencode")]
    out["held"] = [item("claude"), item("codex", [
        held("profile/deep", "gpt-6-astra", "high", "gpt-6.2-astra"),
        cell("profile/top", "gpt-6-astra", "xhigh")]), item("opencode")]
    out["next"] = [item("claude"), item("codex", plain_codex()), item("opencode", [
        cell("profile/top", "opencode-go/glm-5.3-flash", "max"),
        cell("tier/deep", "opencode-go/glm-5.3-flash", "max", following=upcoming("opencode-go/glm-5.4-flash", "max"),
             lines=[{"key": "CFG_V2_AUTO_FALLBACKS", "from": "opencode-go/glm-5.3-flash:max",
                     "to": "opencode-go/glm-5.4-flash:max"}])])]
    out["proposal"] = [item("claude"), item("codex", [
        cell("profile/balanced", "gpt-6.1-terra", "xhigh",
             following=upcoming("gpt-7-terra", "xhigh", "proposal", "other family")),
        cell("profile/deep", "gpt-6-astra", "high")]), item("opencode")]
    out["today"] = [item("claude"), item("codex", plain_codex(), cli=CLI_PREVIOUS), item("opencode", [
        cell("profile/top", "opencode-go/glm-5.3-flash", "max"),
        cell("tier/deep", "opencode-go/glm-5.3-flash", "max", following=upcoming("opencode-go/glm-5.4-flash", "max"))])]
    out["mixed"] = [
        item("claude"),
        item("codex", [followed("profile/top", "gpt-6-astra", "gpt-6.2-astra", "xhigh"),
                       held("profile/deep", "gpt-6-astra", "high", "gpt-6.2-astra"),
                       kept("profile/mini", "gpt-6-luna", "high"),
                       cell("profile/balanced", "gpt-6.1-terra", "xhigh",
                            following=upcoming("gpt-7-terra", "xhigh", "proposal", "other family")),
                       cell("profile/light", "gpt-6-luna", "xhigh",
                            following=upcoming("gpt-6.1-luna", "xhigh")),
                       cell("profile/balanced-deep", "gpt-6.1-sol", "xhigh",
                            following=upcoming("gpt-6.2-sol", "xhigh"),
                            lines=[{"key": "CFG_TIER_DEEP_FAILOVER_CASCADE", "from": "gpt-6.1-sol:xhigh gpt-6-luna:medium",
                                    "to": "gpt-6.2-sol:xhigh gpt-6-luna:medium"}])],
             cli=CLI_PREVIOUS),
        item("opencode", [followed("tier/deep", "opencode-go/glm-5.2-flash", "opencode-go/glm-5.3-flash", "max")])]
    return out


def _redate(answer, date):
    import copy
    out = copy.deepcopy(answer)
    for entry in out:
        for found in entry["cells"]:
            if found["why"] == "followed":
                found["since"]["date"] = date
    return out
