"""
Core logic for copying Claude desktop Code sessions between accounts / organizations and for
converting a claude.ai data export into Markdown.

How the Claude desktop app stores Code sessions:
    <app data>/claude-code-sessions/<account-uuid>/<org-uuid>/local_<id>.json   session metadata
    <app data>/claude-code-sessions/<account-uuid>/<org-uuid>/archived-sessions.idx
    <claude config>/projects/<cwd-slug>/<cli-session-id>.jsonl                   transcript
    <claude config>/projects/<cwd-slug>/<cli-session-id>/                        sidecar files

The transcript is not tied to an account, the metadata is. Copying a session clones the metadata
into the target <account>/<org> folder and clones the transcript under a fresh id.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

Log = Callable[[str], None]

# Session fields that point at org/account-scoped state (connectors, environments, other sessions,
# cached prompts) or are transient UI/error state. They are dropped from the copy; the app rebuilds them.
DROP_FIELDS = {
    "remoteMcpServersConfig", "enabledMcpTools", "envScopeId", "startedFromEnvironmentId",
    "error", "errorAt", "errorRows", "priorErrorMark", "interruptedAwaitingPerson",
    "sideSessionNotes", "backgroundTaskSuggestions", "resolvedBackgroundTaskSuggestions",
    "peerReceipts", "peerMessaged", "sideSessionWakesSinceUserMessage", "steeredByRemoteClient",
    "remoteControlAutoEligible", "lastTurnCutShort", "latestUserFrameAt", "lastSpawnRootDetected",
    "spawnSeed", "promptAppendSnapshot", "toolSurfaceSnapshot", "turnBoxDeclared", "turnBoxMounted",
    "bridgeSessionId", "remoteSessionId", "parentSessionId", "queuedSideSessionNotes", "pendingSystemReminder",
}

# Fields that link a session to the session it was started from (sub-sessions and forks).
PARENT_FIELDS = ("spawnedFrom", "forkedFromSessionId")


# ---------------------------------------------------------------- locations

def default_app_dir() -> Path:
    if env := os.environ.get("CLAUDE_APP_DIR"):
        return Path(env)
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Claude"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "Claude"
    return Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "Claude"


def default_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))


@dataclass
class Paths:
    app_dir: Path = field(default_factory=default_app_dir)
    config_dir: Path = field(default_factory=default_config_dir)

    @property
    def sessions_dir(self) -> Path:
        return self.app_dir / "claude-code-sessions"

    @property
    def projects_dir(self) -> Path:
        return self.config_dir / "projects"


# ---------------------------------------------------------------- model

@dataclass
class Session:
    path: Path
    data: dict
    archived: bool

    @property
    def id(self) -> str:
        return self.data.get("sessionId") or self.path.stem

    @property
    def title(self) -> str:
        return self.data.get("title") or "(untitled)"

    @property
    def cwd(self) -> str:
        return self.data.get("cwd") or ""

    @property
    def last_activity(self) -> int:
        return self.data.get("lastActivityAt") or self.data.get("createdAt") or 0

    @property
    def parent_id(self) -> str | None:
        """The session this one was started from (a sub-session) or forked from, if any."""
        spawned = self.data.get("spawnedFrom")
        if isinstance(spawned, dict) and spawned.get("sessionId"):
            return spawned["sessionId"]
        return self.data.get("forkedFromSessionId") or None


@dataclass
class Org:
    account: str
    uuid: str
    path: Path
    sessions: list[Session]

    @property
    def label(self) -> str:
        n = len(self.sessions)
        recent = f' - latest: "{self.sessions[0].title[:40]}"' if self.sessions else ""
        return f"org {self.uuid[:8]}  ({n} session{'s' if n != 1 else ''}){recent}"


@dataclass
class Account:
    uuid: str
    signed_in: bool
    orgs: list[Org]


def fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M") if ms else "-"


def _read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json_atomic(p: Path, data) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def archived_ids(org_dir: Path) -> set[str]:
    data = _read_json(org_dir / "archived-sessions.idx", {}) or {}
    return set(data.get("archived") or [])


def load_sessions(org_dir: Path) -> list[Session]:
    arch = archived_ids(org_dir)
    out = []
    for f in org_dir.glob("local_*.json"):
        data = _read_json(f)
        if isinstance(data, dict):
            out.append(Session(f, data, bool(data.get("isArchived")) or data.get("sessionId") in arch))
    out.sort(key=lambda s: s.last_activity, reverse=True)
    return out


def list_accounts(paths: Paths) -> list[Account]:
    if not paths.sessions_dir.is_dir():
        return []
    cfg = _read_json(paths.app_dir / "config.json", {}) or {}
    current = cfg.get("lastKnownAccountUuid")
    accounts = []
    for acct in sorted(p for p in paths.sessions_dir.iterdir() if p.is_dir()):
        orgs = [Org(acct.name, o.name, o, load_sessions(o))
                for o in sorted(p for p in acct.iterdir() if p.is_dir())]
        accounts.append(Account(acct.name, acct.name == current, orgs))
    accounts.sort(key=lambda a: not a.signed_in)
    return accounts


def resolve_org(paths: Paths, spec: str) -> Path:
    """Resolve 'ACCOUNT/ORG' (unique uuid prefixes allowed) to an org folder."""
    if "/" not in spec:
        raise ValueError(f"expected ACCOUNT/ORG, got '{spec}'")
    acct, org = spec.split("/", 1)

    def pick(parent: Path, prefix: str, what: str) -> Path:
        if not parent.is_dir():
            raise ValueError(f"{parent} does not exist")
        hits = [p for p in parent.iterdir() if p.is_dir() and p.name.startswith(prefix)]
        if not hits:
            raise ValueError(f"no {what} matching '{prefix}'")
        if len(hits) > 1:
            raise ValueError(f"'{prefix}' is ambiguous: {', '.join(h.name for h in hits)}")
        return hits[0]

    return pick(pick(paths.sessions_dir, acct, "account"), org, "org")


# ---------------------------------------------------------------- session trees

def children_map(sessions: Iterable[Session]) -> dict[str, list[Session]]:
    """parent id -> direct sub-sessions, for parents present in `sessions`."""
    sessions = list(sessions)
    ids = {s.id for s in sessions}
    kids: dict[str, list[Session]] = {}
    for s in sessions:
        if s.parent_id in ids and s.parent_id != s.id:
            kids.setdefault(s.parent_id, []).append(s)
    return kids


def with_descendants(all_sessions: Iterable[Session], selected_ids: Iterable[str]) -> list[Session]:
    """The selected sessions plus every sub-session below them, parents before children."""
    all_sessions = list(all_sessions)
    by_id = {s.id: s for s in all_sessions}
    kids = children_map(all_sessions)
    out: list[Session] = []
    seen: set[str] = set()

    def visit(sid: str) -> None:
        if sid in seen or sid not in by_id:
            return
        seen.add(sid)
        out.append(by_id[sid])
        for k in kids.get(sid, []):
            visit(k.id)

    for sid in selected_ids:
        visit(sid)
    return out


# ---------------------------------------------------------------- copy

def cwd_slug(cwd: str) -> str:
    # Claude Code names project folders after the cwd with every non-alphanumeric char -> '-'
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def find_transcript(paths: Paths, cli_id: str, cwd: str | None) -> Path | None:
    if cwd:
        p = paths.projects_dir / cwd_slug(cwd) / f"{cli_id}.jsonl"
        if p.exists():
            return p
    if not paths.projects_dir.is_dir():
        return None
    return next(iter(paths.projects_dir.glob(f"*/{cli_id}.jsonl")), None)


def _clone_transcript(old_id: str, new_id: str, src: Path) -> None:
    dst = src.with_name(f"{new_id}.jsonl")
    with src.open("r", encoding="utf-8") as fin, dst.open("w", encoding="utf-8") as fout:
        for line in fin:
            fout.write(line.replace(old_id, new_id))
    side = src.with_name(old_id)
    if side.is_dir():
        new_side = src.with_name(new_id)
        shutil.copytree(side, new_side)
        for p in new_side.rglob("*"):
            if p.is_file() and p.suffix in (".json", ".jsonl", ".txt", ".md"):
                try:
                    p.write_text(p.read_text(encoding="utf-8").replace(old_id, new_id), encoding="utf-8")
                except UnicodeDecodeError:
                    pass


def copy_sessions(paths: Paths, src_dir: Path, dst_dir: Path, sessions: Iterable[Session], *,
                  share_transcript: bool = False, title_suffix: str = "", dry_run: bool = False,
                  known_ids: dict[str, str] | None = None, written: dict[str, str] | None = None,
                  log: Log = print) -> int:
    """Copy `sessions` from src_dir to dst_dir. Returns the number of sessions copied.

    `known_ids` maps source ids copied earlier to their ids in the target org, so a sub-session that
    arrives after its parent was copied still points at the parent's copy. `written`, if given, is
    filled with {source id: new id} for this call. Both are used by the watcher.
    """
    if src_dir.resolve() == dst_dir.resolve():
        raise ValueError("source and target are the same org")
    sessions = list(sessions)
    prefix = "[dry run] " if dry_run else ""
    log(f"{prefix}Copying {len(sessions)} session(s)\n  from {src_dir}\n  to   {dst_dir}")

    # New ids up front, so sub-sessions can point at their parent's copy.
    new_ids = {s.id: f"local_{uuid.uuid4()}" for s in sessions}
    lookup = {**(known_ids or {}), **new_ids}
    if written is not None:
        written.update(new_ids)

    copied, newly_archived = 0, []
    for s in sessions:
        new_local = new_ids[s.id]
        new = {k: v for k, v in s.data.items() if k not in DROP_FIELDS}
        new["sessionId"] = new_local
        parent = s.parent_id
        if parent in lookup:
            if isinstance(new.get("spawnedFrom"), dict):
                new["spawnedFrom"] = {**new["spawnedFrom"], "sessionId": lookup[parent]}
            if new.get("forkedFromSessionId"):
                new["forkedFromSessionId"] = lookup[parent]
        else:
            # The parent isn't being copied, so it won't exist in the target org.
            for k in PARENT_FIELDS:
                new.pop(k, None)
        if title_suffix:
            new["title"] = s.title + title_suffix
        new["copiedFrom"] = {"sessionId": s.id, "org": src_dir.name, "account": src_dir.parent.name,
                             "at": int(time.time() * 1000)}

        note = "no transcript id"
        old_cli = s.data.get("cliSessionId")
        if old_cli:
            src_tr = find_transcript(paths, old_cli, s.cwd)
            if src_tr is None:
                note = "transcript NOT found (metadata only)"
            elif share_transcript:
                note = "synced: shares the original transcript"
            else:
                new_cli = str(uuid.uuid4())
                if not dry_run:
                    _clone_transcript(old_cli, new_cli, src_tr)
                new["cliSessionId"] = new_cli
                note = "transcript cloned"

        if not dry_run:
            _write_json_atomic(dst_dir / f"{new_local}.json", new)
        if s.archived:
            newly_archived.append(new_local)
        copied += 1
        log(f"  {'  - ' if parent in lookup else '+ '}{s.title}  ({note})")

    if newly_archived and not dry_run:
        idx = dst_dir / "archived-sessions.idx"
        data = _read_json(idx, None) or {"v": 1, "archived": []}
        data["archived"] = list(dict.fromkeys((data.get("archived") or []) + newly_archived))
        _write_json_atomic(idx, data)

    log(f"{prefix}{'Would copy' if dry_run else 'Copied'} {copied} session(s).")
    if not dry_run and copied:
        log("Restart the Claude desktop app and switch to the target account/org to see them.")
    return copied


# ---------------------------------------------------------------- live sync (watch)
#
# Change detection is a hash tree, like git: one sha256 per session file, and per org one sha256 over
# the sorted session hashes. An unchanged org is a single string compare. The state lives in
# <app data>/claude-buddy/watch-state.json:
#   sessions  {session file path: sha256}
#   orgs      {org folder: org hash}
#   copied    {target org: {source session id: id of its copy}}   what the watcher mirrored, and where
#   created   [ids the watcher wrote]                              skipped as sources, so A<->B cannot loop

STATE_VERSION = 1


def watch_state_path(paths: Paths) -> Path:
    return paths.app_dir / "claude-buddy" / "watch-state.json"


def load_watch_state(path: Path) -> dict:
    data = _read_json(path, None)
    if not isinstance(data, dict) or data.get("v") != STATE_VERSION:
        data = {"v": STATE_VERSION, "sessions": {}, "orgs": {}, "copied": {}, "created": []}
    for key, empty in (("sessions", {}), ("orgs", {}), ("copied", {}), ("created", [])):
        data.setdefault(key, empty)
    return data


def save_watch_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(path, state)


def file_hash(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def session_hashes(org_dir: Path) -> dict[str, str]:
    """{session file path: sha256} for every local_*.json in the org."""
    out = {}
    for f in sorted(org_dir.glob("local_*.json")):
        try:
            out[str(f)] = file_hash(f)
        except OSError:
            pass  # written or removed mid-scan; the next pass sees it
    return out


def org_hash(hashes: dict[str, str]) -> str:
    h = hashlib.sha256()
    for path in sorted(hashes):
        h.update(f"{path}\0{hashes[path]}\n".encode("utf-8"))
    return h.hexdigest()


def watch_once(paths: Paths, src_dir: Path, dst_dir: Path, state: dict, *, log: Log = print) -> int:
    """One pass: mirror every session new to src_dir into dst_dir, sharing the transcript.

    Returns the number of sessions copied. Mutates `state`; the caller saves it.
    """
    hashes = session_hashes(src_dir)
    src_key, dst_key = str(src_dir), str(dst_dir)
    current = org_hash(hashes)
    if state["orgs"].get(src_key) == current:
        return 0

    if dst_key not in state["copied"]:
        # First time this target is seen (or the state file was lost): rebuild what is already mirrored
        # from the copiedFrom markers in the target, so nothing is copied twice.
        seeded = {s.data["copiedFrom"]["sessionId"]: s.id for s in load_sessions(dst_dir)
                  if isinstance(s.data.get("copiedFrom"), dict) and s.data["copiedFrom"].get("org") == src_dir.name}
        state["copied"][dst_key] = seeded
        state["created"] = sorted(set(state["created"]) | set(seeded.values()))
    copied_here: dict[str, str] = state["copied"][dst_key]
    created = set(state["created"])
    sessions = load_sessions(src_dir)
    new = [s for s in sessions
           if s.id not in copied_here          # not mirrored to this target yet
           and s.id not in created             # not something a watcher wrote (A<->B loop guard)
           and not s.data.get("copiedFrom")]   # not a copy made by `copy` or a split of a synced session
    # Parents before children, so a parent copied in this pass is known when its sub-session is written.
    new = with_descendants(sessions, [s.id for s in new]) if new else []
    new = [s for s in new if s.id not in copied_here and s.id not in created and not s.data.get("copiedFrom")]

    n = 0
    if new:
        written: dict[str, str] = {}
        n = copy_sessions(paths, src_dir, dst_dir, new, share_transcript=True, known_ids=copied_here,
                          written=written, log=log)
        copied_here.update(written)
        state["created"] = sorted(created | set(written.values()))

    for path, h in hashes.items():
        state["sessions"][path] = h
    for path in [p for p in state["sessions"] if p.startswith(src_key + os.sep) and p not in hashes]:
        del state["sessions"][path]
    state["orgs"][src_key] = current
    return n


def watch(paths: Paths, src_dir: Path, dst_dir: Path, *, both: bool = False, interval: float = 2.0,
          debounce: float = 0.5, once: bool = False, state_path: Path | None = None,
          should_stop: Callable[[], bool] | None = None, log: Log = print) -> int:
    """Poll the source org (and the target too with `both`) and mirror new sessions as they appear.

    Zero dependencies: plain polling every `interval` seconds. A change is applied `debounce` seconds
    after it is first seen, so a session file that is still being written is read whole. Returns the
    total number of sessions copied.
    """
    if src_dir.resolve() == dst_dir.resolve():
        raise ValueError("source and target are the same org")
    state_path = state_path or watch_state_path(paths)
    state = load_watch_state(state_path)
    pairs = [(src_dir, dst_dir)] + ([(dst_dir, src_dir)] if both else [])
    total = 0
    log(f"Watching {src_dir}{' <-> ' if both else ' -> '}{dst_dir}  (every {interval:g}s, state in {state_path})")
    while True:
        changed = [p for p in pairs if state["orgs"].get(str(p[0])) != org_hash(session_hashes(p[0]))]
        if changed:
            if debounce and not once:
                time.sleep(debounce)
            for s, d in changed:
                total += watch_once(paths, s, d, state, log=log)
            save_watch_state(state_path, state)
        if once or (should_stop and should_stop()):
            return total
        time.sleep(interval)


# ---------------------------------------------------------------- claude.ai export -> markdown

def _msg_text(m: dict) -> str:
    parts = []
    for c in m.get("content") or []:
        t = c.get("type")
        if t == "text" and c.get("text"):
            parts.append(c["text"])
        elif t == "tool_use":
            parts.append(f"*[tool call: {c.get('name')}]*")
        elif t == "tool_result":
            parts.append("*[tool result]*")
        # "thinking" blocks are skipped
    if not parts and m.get("text"):
        parts.append(m["text"])
    for a in m.get("attachments") or []:
        body, name = a.get("extracted_content"), a.get("file_name") or "attachment"
        parts.append(f"<details><summary>Attachment: {name}</summary>\n\n{body}\n\n</details>" if body
                     else f"*[attachment: {name}]*")
    for fl in m.get("files") or m.get("files_v2") or []:
        parts.append(f"*[file: {fl.get('file_name') or fl.get('file_uuid')}]*")
    return "\n\n".join(parts).strip()


def _safe(name: str) -> str:
    return (re.sub(r"[^\w\- .]+", "", name).strip() or "untitled")[:80]


def _load_export(path: Path) -> tuple[list, list]:
    def read(name):
        if path.is_dir():
            hits = list(path.rglob(name))
            return json.loads(hits[0].read_text(encoding="utf-8")) if hits else []
        with zipfile.ZipFile(path) as z:
            hits = [n for n in z.namelist() if n.endswith(name)]
            return json.loads(z.read(hits[0]).decode("utf-8")) if hits else []
    return read("conversations.json"), read("projects.json")


def export_to_markdown(export: Path, out: Path, log: Log = print) -> tuple[int, int]:
    if not export.exists():
        raise ValueError(f"{export} not found")
    convs, projects = _load_export(export)
    if not convs and not projects:
        raise ValueError("no conversations.json / projects.json found in the export")
    (out / "chats").mkdir(parents=True, exist_ok=True)

    index = ["# Claude chat export", ""]
    for c in sorted(convs, key=lambda c: c.get("updated_at") or "", reverse=True):
        name = c.get("name") or "untitled"
        date = (c.get("created_at") or "")[:10]
        fname = f"{date} {_safe(name)} {(c.get('uuid') or '')[:8]}.md".strip()
        lines = [f"# {name}", "", f"- created: {c.get('created_at')}", f"- updated: {c.get('updated_at')}",
                 f"- original id: {c.get('uuid')}", ""]
        for m in c.get("chat_messages") or []:
            text = _msg_text(m)
            if text:
                lines += [f"## {'User' if m.get('sender') == 'human' else 'Claude'}", "", text, ""]
        (out / "chats" / fname).write_text("\n".join(lines), encoding="utf-8")
        index.append(f"- [{name}](chats/{fname.replace(' ', '%20')}) - {date}")

    for p in projects:
        pdir = out / "projects" / _safe(p.get("name") or p.get("uuid") or "project")
        pdir.mkdir(parents=True, exist_ok=True)
        meta = [f"# {p.get('name')}", "", p.get("description") or "", ""]
        if p.get("prompt_template"):
            meta += ["## Project instructions", "", p["prompt_template"], ""]
        (pdir / "_project.md").write_text("\n".join(meta), encoding="utf-8")
        for d in p.get("docs") or []:
            (pdir / _safe(d.get("filename") or d.get("uuid") or "doc")).write_text(d.get("content") or "",
                                                                                   encoding="utf-8")

    (out / "INDEX.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    log(f"Wrote {len(convs)} chat(s) and {len(projects)} project(s) to {out}")
    return len(convs), len(projects)
