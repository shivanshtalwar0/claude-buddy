import json
import tempfile
import unittest
from pathlib import Path

from claude_buddy.cli import main
from claude_buddy.core import (Paths, cwd_slug, load_sessions, load_watch_state, org_hash, session_hashes, watch,
                               watch_once)

CLI_A = "aaaaaaaa-0000-0000-0000-000000000001"
CLI_B = "bbbbbbbb-0000-0000-0000-000000000002"
CWD = "/home/me/project"


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = Paths(root / "app", root / "cfg")
        self.src = self.paths.sessions_dir / "acctA" / "orgA"
        self.dst = self.paths.sessions_dir / "acctB" / "orgB"
        self.src.mkdir(parents=True)
        self.dst.mkdir(parents=True)
        (self.paths.app_dir / "config.json").write_text(json.dumps({"lastKnownAccountUuid": "acctA"}))
        self.proj = self.paths.projects_dir / cwd_slug(CWD)
        self.proj.mkdir(parents=True)
        self.state_file = root / "state.json"
        self.log = []

    def tearDown(self):
        self.tmp.cleanup()

    def _session(self, org: Path, sid: str, title: str, cli: str | None = None, **extra) -> Path:
        data = {"sessionId": sid, "cliSessionId": cli, "cwd": CWD, "title": title, "lastActivityAt": 1, **extra}
        p = org / f"{sid}.json"
        p.write_text(json.dumps(data))
        if cli:
            (self.proj / f"{cli}.jsonl").write_text(json.dumps({"sessionId": cli}) + "\n")
        return p

    def _watch(self, **kw) -> int:
        return watch(self.paths, self.src, self.dst, once=True, state_path=self.state_file, log=self.log.append, **kw)

    # ---------------------------------------------------------------- hash tree

    def test_org_hash_is_over_sorted_session_hashes(self):
        self._session(self.src, "local_b", "B")
        self._session(self.src, "local_a", "A")
        h1 = org_hash(session_hashes(self.src))
        self.assertEqual(h1, org_hash(dict(reversed(list(session_hashes(self.src).items())))))
        (self.src / "local_a.json").write_text(json.dumps({"sessionId": "local_a", "title": "A renamed"}))
        self.assertNotEqual(h1, org_hash(session_hashes(self.src)))
        self.assertEqual(org_hash({}), org_hash({}))

    def test_unchanged_org_is_a_single_compare(self):
        self._session(self.src, "local_a", "A", CLI_A)
        self.assertEqual(self._watch(), 1)
        state = load_watch_state(self.state_file)
        self.assertEqual(state["orgs"][str(self.src)], org_hash(session_hashes(self.src)))
        # Second pass: org hash matches, nothing is loaded or copied.
        self.assertEqual(watch_once(self.paths, self.src, self.dst, state, log=self.log.append), 0)
        self.assertEqual(len(load_sessions(self.dst)), 1)

    # ---------------------------------------------------------------- mirroring

    def test_new_session_is_mirrored_with_a_shared_transcript(self):
        self._session(self.src, "local_a", "First", CLI_A)
        self.assertEqual(self._watch(), 1)
        (copy,) = load_sessions(self.dst)
        self.assertEqual(copy.data["cliSessionId"], CLI_A)          # synced, not cloned
        self.assertEqual(copy.data["copiedFrom"]["sessionId"], "local_a")
        self.assertEqual(len(list(self.proj.glob("*.jsonl"))), 1)    # original transcript untouched, none cloned
        self.assertEqual(len(load_sessions(self.src)), 1)             # source org untouched

        # A session that appears later is picked up; the earlier one is not copied again.
        self._session(self.src, "local_b", "Second", CLI_B)
        self.assertEqual(self._watch(), 1)
        self.assertEqual(sorted(s.data["copiedFrom"]["sessionId"] for s in load_sessions(self.dst)),
                         ["local_a", "local_b"])

    def test_later_sub_session_points_at_the_parents_copy(self):
        self._session(self.src, "local_p", "Parent", CLI_A)
        self._watch()
        parent_copy = load_sessions(self.dst)[0].id

        self._session(self.src, "local_c", "Child", CLI_B, spawnedFrom={"sessionId": "local_p", "x": 1})
        self._watch()
        child_copy = next(s for s in load_sessions(self.dst) if s.data["copiedFrom"]["sessionId"] == "local_c")
        self.assertEqual(child_copy.data["spawnedFrom"], {"sessionId": parent_copy, "x": 1})

    def test_changed_session_is_not_copied_twice(self):
        p = self._session(self.src, "local_a", "First", CLI_A)
        self._watch()
        p.write_text(json.dumps({**json.loads(p.read_text()), "title": "First, renamed"}))
        self.assertEqual(self._watch(), 0)
        self.assertEqual(len(load_sessions(self.dst)), 1)

    def test_copies_and_splits_are_never_sources(self):
        # A session that `copy` made (copiedFrom set) is left alone when its org is watched.
        self._session(self.src, "local_x", "Copied in", CLI_A, copiedFrom={"sessionId": "local_elsewhere"})
        self.assertEqual(self._watch(), 0)
        self.assertEqual(load_sessions(self.dst), [])

    # ---------------------------------------------------------------- both directions

    def test_both_directions_do_not_loop(self):
        self._session(self.src, "local_a", "From A", CLI_A)
        self._session(self.dst, "local_b", "From B", CLI_B)
        self.assertEqual(self._watch(both=True), 2)
        self.assertEqual(self._watch(both=True), 0)
        self.assertEqual(self._watch(both=True), 0)
        self.assertEqual(len(load_sessions(self.src)), 2)
        self.assertEqual(len(load_sessions(self.dst)), 2)
        state = load_watch_state(self.state_file)
        self.assertEqual(len(state["created"]), 2)
        # The watcher's own copies are in the skip set.
        for org in (self.src, self.dst):
            for s in load_sessions(org):
                if s.data.get("copiedFrom"):
                    self.assertIn(s.id, state["created"])

    # ---------------------------------------------------------------- state file and CLI

    def test_state_survives_restart_and_bad_files(self):
        self._session(self.src, "local_a", "A", CLI_A)
        self._watch()
        first = json.loads(self.state_file.read_text())
        self.assertEqual(first["v"], 1)
        self.assertEqual(set(first), {"v", "sessions", "orgs", "copied", "created"})
        # Restart with the saved state: nothing new.
        self.assertEqual(self._watch(), 0)
        # A corrupt or missing state file is rebuilt from the copiedFrom markers in the target,
        # so the already-mirrored session is not copied a second time.
        self.state_file.write_text("{not json")
        self.assertEqual(load_watch_state(self.state_file)["sessions"], {})
        self.assertEqual(self._watch(), 0)
        self.assertEqual(len(load_sessions(self.dst)), 1)
        state = load_watch_state(self.state_file)
        self.assertEqual(list(state["copied"][str(self.dst)]), ["local_a"])
        self.assertEqual(state["created"], [load_sessions(self.dst)[0].id])

    def test_same_org_is_an_error(self):
        with self.assertRaises(ValueError):
            watch(self.paths, self.src, self.src, once=True, state_path=self.state_file, log=self.log.append)

    def test_cli_once(self):
        self._session(self.src, "local_a", "A", CLI_A)
        main(["--app-dir", str(self.paths.app_dir), "--config-dir", str(self.paths.config_dir),
              "watch", "--from", "acctA/orgA", "--to", "acctB/orgB", "--once", "--state", str(self.state_file)])
        self.assertEqual(len(load_sessions(self.dst)), 1)
        self.assertTrue(self.state_file.exists())


if __name__ == "__main__":
    unittest.main()
