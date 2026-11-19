from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from dmo.settings import game_directory


class GameDirectoryTests(TestCase):
    def test_explicit_installation_with_spaces(self):
        with patch.dict('os.environ', {'DMO_GAME_DIR': '/tmp/my game/Release'}, clear=True):
            self.assertEqual(game_directory(), Path('/tmp/my game/Release'))

    def test_default_uses_desktop_home(self):
        with (patch.dict('os.environ', {}, clear=True),
              patch('dmo.settings.Path.home', return_value=Path('/tmp/player'))):
            self.assertEqual(game_directory(), Path('/tmp/player/Games/DMO/DMO/DMO/Release'))

    def test_direct_sudo_monitor_uses_invoking_users_home(self):
        with (patch.dict('os.environ', {'SUDO_USER': 'player'}, clear=True),
              patch('dmo.settings.os.geteuid', return_value=0),
              patch('dmo.settings.pwd.getpwnam', return_value=SimpleNamespace(pw_dir='/tmp/player'))):
            self.assertEqual(game_directory(), Path('/tmp/player/Games/DMO/DMO/DMO/Release'))

    def test_unknown_sudo_user_falls_back_to_current_home(self):
        with (patch.dict('os.environ', {'SUDO_USER': 'missing'}, clear=True),
              patch('dmo.settings.os.geteuid', return_value=0),
              patch('dmo.settings.pwd.getpwnam', side_effect=KeyError),
              patch('dmo.settings.Path.home', return_value=Path('/tmp/fallback'))):
            self.assertEqual(game_directory(), Path('/tmp/fallback/Games/DMO/DMO/DMO/Release'))
