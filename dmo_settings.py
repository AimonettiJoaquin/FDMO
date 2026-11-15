"""Installation paths shared by the desktop runner and read-only monitor."""

import os
from pathlib import Path
import pwd


def game_directory():
    """Use an explicit installation or the invoking desktop user's home."""
    configured = os.environ.get('DMO_GAME_DIR')
    if configured:
        return Path(configured).expanduser().resolve()
    home = Path.home()
    if os.geteuid() == 0 and os.environ.get('SUDO_USER'):
        try:
            home = Path(pwd.getpwnam(os.environ['SUDO_USER']).pw_dir)
        except KeyError:
            pass
    return home / 'Games' / 'DMO' / 'DMO' / 'DMO' / 'Release'
