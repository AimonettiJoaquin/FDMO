from unittest import TestCase
from unittest.mock import Mock, patch
from PIL import Image

from dmo_window import WindowFrame, WindowInput, WindowDesktop


class WindowTests(TestCase):
    def frame(self):
        return WindowFrame(dict(address='0x1234', at=[100, 30], size=[960, 540]), (1920, 1080))

    def test_input_maps_window_origin_and_size_in_both_directions(self):
        frame = self.frame()
        with patch('dmo_window.hypr_json', return_value=dict(address='0x1234', at=[100, 30], size=[960, 540])):
            desktop = WindowInput(frame)
            desktop.raw = Mock()
            desktop.raw.move.return_value = (580, 444)
            desktop.raw.position.return_value = (580, 444)
            self.assertEqual(desktop.move((960, 828)), (960, 828))
            desktop.raw.move.assert_called_once_with((580, 444))
            self.assertEqual(desktop.position(), (960, 828))
            desktop.click()
            desktop.raw.click.assert_called_once_with()

    def test_focus_loss_or_resize_prevents_input(self):
        for active in (dict(address='0xother'), dict(address='0x1234', at=[100, 30], size=[1000, 540])):
            with patch('dmo_window.hypr_json', return_value=active):
                desktop = WindowInput(self.frame())
                desktop.raw = Mock()
                with self.assertRaises(RuntimeError):
                    desktop.move((960, 828))
                desktop.raw.move.assert_not_called()

    def test_focus_uses_the_hyprland_general_focus_dispatcher(self):
        frame = self.frame()
        with patch.object(frame, 'validate') as validate, patch('dmo_window.time.sleep'), \
                patch('dmo_window.subprocess.run') as run:
            frame.focus()
        self.assertEqual(run.call_args.args[0][2], 'hl.dsp.focus({ window = "address:0x1234" })')
        validate.assert_called_once_with()

    def test_discovery_rejects_ambiguous_game_windows(self):
        with patch('dmo_window.hypr_json', return_value=[dict(mapped=True, title='DMO')] * 2):
            with self.assertRaisesRegex(RuntimeError, 'única'):
                WindowFrame.discover(dict(screen=dict(width=1920, height=1080)))

    def test_capture_crops_window_and_normalizes_reference_size(self):
        desktop = WindowDesktop(Mock(), self.frame())
        with patch.object(desktop.frame, 'validate'), patch('dmo_window.subprocess.run') as run, \
                patch('dmo_window.Image.open') as opened:
            opened.return_value.__enter__.return_value = Image.new('RGB', (960, 540), 'green')
            result = desktop.screenshot()
        self.assertEqual(result.size, (1920, 1080))
        self.assertEqual(run.call_args.args[0][1:3], ['-g', '100,30 960x540'])

    def test_black_game_capture_is_reported_separately(self):
        desktop = WindowDesktop(Mock(), self.frame())
        with patch.object(desktop.frame, 'validate'), patch('dmo_window.subprocess.run'), \
                patch('dmo_window.Image.open') as opened:
            opened.return_value.__enter__.return_value = Image.new('RGB', (960, 540), 'black')
            with self.assertRaisesRegex(RuntimeError, 'en negro'):
                desktop.screenshot()
