from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import libotctl


class LibotctlPidSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.spec = libotctl.ModuleSpec(
            name="test",
            log_file=root / "test.log",
            pid_file=root / "test.pid",
            command=("python", "-m", "example"),
            identity_markers=("-m", "example"),
            redirect_output=True,
            cwd=root,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_pid(self, pid: int = 12345) -> None:
        self.spec.pid_file.write_text(str(pid), encoding="utf-8")

    def test_unknown_pid_visibility_preserves_pid_file(self) -> None:
        self._write_pid()
        with patch.object(
            libotctl,
            "_process_state",
            return_value=libotctl.ProcessState.UNKNOWN,
        ):
            state = libotctl._cleanup_stale_pidfile(self.spec)
        self.assertIs(state, libotctl.ProcessState.UNKNOWN)
        self.assertTrue(self.spec.pid_file.exists())

    def test_confirmed_dead_pid_is_cleaned(self) -> None:
        self._write_pid()
        with patch.object(
            libotctl,
            "_process_state",
            return_value=libotctl.ProcessState.DEAD,
        ):
            state = libotctl._cleanup_stale_pidfile(self.spec)
        self.assertIs(state, libotctl.ProcessState.DEAD)
        self.assertFalse(self.spec.pid_file.exists())

    def test_restart_does_not_start_when_stop_is_unsafe(self) -> None:
        with patch.object(libotctl, "_stop", return_value=1), patch.object(
            libotctl,
            "_start",
        ) as start:
            self.assertEqual(libotctl._restart(self.spec), 1)
        start.assert_not_called()

    def test_stop_refuses_in_invisible_pid_namespace(self) -> None:
        self._write_pid()
        with patch.object(
            libotctl,
            "_process_state",
            return_value=libotctl.ProcessState.UNKNOWN,
        ), patch.object(libotctl.os, "killpg") as killpg:
            self.assertEqual(libotctl._stop(self.spec), 1)
        killpg.assert_not_called()
        self.assertTrue(self.spec.pid_file.exists())

    def test_identity_check_requires_markers_cwd_and_group_leader(self) -> None:
        with patch.object(
            libotctl,
            "_read_process_cmdline",
            return_value=("python", "-m", "example"),
        ), patch.object(
            libotctl,
            "_read_process_cwd",
            return_value=self.spec.cwd,
        ), patch.object(libotctl.os, "getpgid", return_value=12345):
            self.assertIsNone(libotctl._managed_process_problem(self.spec, 12345))

        with patch.object(
            libotctl,
            "_read_process_cmdline",
            return_value=("python", "-m", "something_else"),
        ):
            self.assertIn(
                "command line does not match",
                libotctl._managed_process_problem(self.spec, 12345) or "",
            )


if __name__ == "__main__":
    unittest.main()
