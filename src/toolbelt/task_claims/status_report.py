"""``tt task status``: who holds what, as a JSON-able document and as text."""

from datetime import datetime, timedelta

from toolbelt.task_claims.claim_file import Claim, iso
from toolbelt.task_claims.reading import Holder, Reading
from toolbelt.task_claims.resolution import TrackedTask, resolve_claim


def short_duration(duration: timedelta) -> str:
    """``45s``, ``12m``, ``3h05m`` (no sign: callers say "ago" or "left")."""
    seconds = max(0, int(duration.total_seconds()))
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _claim_document(claim: Claim, tasks: list[TrackedTask], *, revoked: bool) -> dict:
    return {
        "kind": claim.kind,
        "path" if claim.kind == "worktree" else "ref": (
            claim.path if claim.kind == "worktree" else claim.ref
        ),
        "touched_at": iso(claim.touched_at),
        "revoked": revoked,
        "resolved": [
            {"repo": task.repo_name, "repo_id": task.repo_id, "branch": task.branch}
            for task in resolve_claim(claim, tasks)
        ],
    }


def holder_document(holder: Holder, tasks: list[TrackedTask], now: datetime) -> dict:
    file = holder.file
    return {
        "holder": holder.holder,
        "kind": file.kind,
        "state": file.state,
        "status": holder.status,
        "live": holder.live,
        "stale": holder.stale,
        "session_id": file.session_id,
        "agent_id": file.agent_id,
        "link": file.link,
        "started_at": iso(file.started_at),
        "updated_at": iso(file.updated_at),
        "age_seconds": int(holder.age(now).total_seconds()),
        "ttl_seconds": file.ttl_seconds,
        "ttl_left_seconds": int(holder.ttl_left(now).total_seconds()),
        "claims": [
            *(_claim_document(c, tasks, revoked=False) for c in holder.claims),
            *(_claim_document(c, tasks, revoked=True) for c in holder.revoked),
        ],
    }


def status_document(reading: Reading, tasks: list[TrackedTask], now: datetime) -> dict:
    return {
        "now": iso(now),
        "holders": [holder_document(h, tasks, now) for h in reading.holders],
        "unreadable": [{"file": s.name, "reason": s.reason} for s in reading.skipped],
    }


def status_text(document: dict, *, verbose: bool) -> str:
    """Live holders with the claims that resolve to a task. ``verbose`` adds
    expired holders, revoked and unresolved claims and ignored files."""
    lines: list[str] = []
    hidden_unresolved = 0
    expired = 0
    for holder in document["holders"]:
        if not holder["live"]:
            expired += 1
            if not verbose:
                continue
        flags = [holder["kind"], holder["state"]]
        if holder["stale"]:
            flags.append("STALE: not heard from, probably hung")
        if not holder["live"]:
            flags = [holder["kind"], "EXPIRED"]
        ttl = (
            f"expires in {short_duration(timedelta(seconds=holder['ttl_left_seconds']))}"
            if holder["live"]
            else f"expired {short_duration(timedelta(seconds=-holder['ttl_left_seconds']))} ago"
        )
        age = short_duration(timedelta(seconds=holder["age_seconds"]))
        head = f"{holder['holder']}  ({', '.join(flags)})  updated {age} ago, {ttl}"
        if holder["link"]:
            head += f"  {holder['link']}"
        lines.append(head)
        for claim in holder["claims"]:
            subject = claim.get("path") or claim.get("ref")
            if claim["revoked"]:
                if verbose:
                    lines.append(f"    {claim['kind']} {subject}  (revoked)")
                continue
            if not claim["resolved"]:
                if verbose:
                    lines.append(f"    {claim['kind']} {subject}  (unresolved)")
                else:
                    hidden_unresolved += 1
                continue
            for task in claim["resolved"]:
                lines.append(
                    f"    {task['repo']}:{task['branch']}  ({claim['kind']} {subject})"
                )
        if not holder["claims"]:
            lines.append("    (no claims)")
    if not lines and not document["unreadable"]:
        lines.append("No live claims.")
    if not verbose and expired:
        lines.append(f"{expired} expired holder(s): `tt task gc` removes them.")
    if not verbose and hidden_unresolved:
        lines.append(
            f"{hidden_unresolved} claim(s) match no tracked task (--verbose lists them)."
        )
    if verbose and document["unreadable"]:
        lines.append("Ignored files:")
        lines.extend(f"    {u['file']}: {u['reason']}" for u in document["unreadable"])
    return "\n".join(lines)
