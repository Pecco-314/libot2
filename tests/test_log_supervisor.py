import gzip
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from src.log_supervisor import RotatingOutput
from unittest.mock import Mock, patch
from dataclasses import replace
from src import libotctl

SCRIPT = Path(__file__).resolve().parents[1] / 'src/log_supervisor.py'


def read_all(path):
    chunks = []
    for p in sorted(path.parent.glob(path.name + '.*.gz')):
        chunks.append(gzip.decompress(p.read_bytes()))
    chunks.append(path.read_bytes())
    return b''.join(chunks)


class LogRotationTests(unittest.TestCase):
    def test_libotctl_uses_supervisor_pid_and_keeps_identity_markers(self):
        with tempfile.TemporaryDirectory() as d:
            spec=replace(libotctl.MODULES['napcat'],log_file=Path(d)/'napcat.log',pid_file=Path(d)/'napcat.pid')
            process=Mock(pid=12345)
            process.poll.return_value=None
            with patch.object(libotctl,'_status',return_value=(libotctl.ProcessState.DEAD,None)), \
                 patch.object(libotctl.subprocess,'Popen',return_value=process) as spawn, \
                 patch.object(libotctl.time,'sleep'):
                self.assertEqual(libotctl._start(spec),0)
            self.assertEqual(spec.pid_file.read_text(),'12345')
            command=spawn.call_args.args[0]
            self.assertIn(str(SCRIPT),command)
            for marker in spec.identity_markers:
                self.assertIn(marker,'\0'.join(command))
            self.assertTrue(spawn.call_args.kwargs['start_new_session'])
            self.assertFalse(spec.log_file.exists())

    def test_byte_exact_rollover_and_existing_archive(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'napcat.log'
            initial = b'old data\n'*100
            path.write_bytes(initial)
            data = ('中文日志\n'*120).encode() + b'X'*503
            output = RotatingOutput(path, 128)
            try:
                for i in range(0,len(data),73):
                    output.write(data[i:i+73])
            finally:
                output.close()
            self.assertEqual(read_all(path),initial+data)
            self.assertLessEqual(path.stat().st_size,128)
            self.assertGreater(len(list(Path(d).glob('*.gz'))),2)

    def test_second_writer_refused(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'napcat.log'
            output=RotatingOutput(path,100)
            try:
                with self.assertRaises(BlockingIOError):
                    RotatingOutput(path,100)
            finally:
                output.close()

    def test_stdout_stderr_and_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'napcat.log'
            result=subprocess.run([sys.executable,str(SCRIPT),'--log',str(path),'--max-bytes','100','--',
                sys.executable,'-c',"import os;os.write(1,b'A'*250);os.write(2,b'error');raise SystemExit(7)"],timeout=15)
            self.assertEqual(result.returncode,7)
            self.assertEqual(read_all(path),b'A'*250+b'error')

    def test_stop_forwards_and_drains_final_output(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'napcat.log'
            child_code="import os,signal,time;signal.signal(signal.SIGTERM,lambda *a:(os.write(2,b'bye\\n'),exit(0)));os.write(1,b'ready\\n');time.sleep(60)"
            p=subprocess.Popen([sys.executable,str(SCRIPT),'--log',str(path),'--',sys.executable,'-c',child_code],start_new_session=True)
            try:
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    if path.exists() and b'ready' in path.read_bytes():break
                    time.sleep(.02)
                self.assertIn(b'ready',path.read_bytes())
                os.killpg(p.pid,signal.SIGTERM)
                p.wait(timeout=15)
                self.assertIn(b'bye\n',read_all(path))
            finally:
                if p.poll() is None:
                    p.terminate();p.wait(timeout=15)
