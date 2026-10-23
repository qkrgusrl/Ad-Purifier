"""동봉 Gemma 실행은 사용자 PATH의 고장 난 junction에 의존하지 않는다."""
import ctypes
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from local_llm import engine


class WindowsServerEnvironmentTests(unittest.TestCase):
    def test_frozen_launch_ignores_untrusted_path_and_restores_parent(self):
        broken = str(Path('untrusted-scoop-gcc'))
        runtime = Path('bundled-runtime').absolute()
        kernel = Mock()
        def get_directory(size, buffer):
            if buffer is not None:
                buffer.value = 'previous-dll-dir'
            return len('previous-dll-dir')
        kernel.GetDllDirectoryW.side_effect = get_directory
        kernel.SetDllDirectoryW.return_value = True
        with patch.object(sys, 'platform', 'win32'), \
                patch.object(sys, 'frozen', True, create=True), \
                patch.object(sys, '_MEIPASS', 'frozen-bundle', create=True), \
                patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
                patch.object(engine, 'llama_server', return_value=runtime / 'llama-server.exe'), \
                patch.dict(os.environ, {'PATH': broken, 'SystemRoot': str(Path('Windows').absolute())}), \
                patch.object(Path, 'resolve', side_effect=OSError('untrusted mount')):
            before = dict(os.environ)
            with engine.external_server_environment() as env:
                self.assertNotIn(broken, env['PATH'])
                self.assertIn(str(runtime), env['PATH'])
                self.assertIn(str(Path(os.environ['SystemRoot']) / 'System32'), env['PATH'])
                self.assertEqual(dict(os.environ), before)
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(kernel.SetDllDirectoryW.call_args_list[0].args, (None,))
        self.assertEqual(kernel.SetDllDirectoryW.call_args_list[-1].args, ('previous-dll-dir',))


if __name__ == '__main__':
    unittest.main()
