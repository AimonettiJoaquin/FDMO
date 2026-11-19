"""Window-relative input and normalized captures for the experimental launcher."""
import json
import re
import subprocess
import time

from PIL import Image

from dmo.bot.dmo_memory_bot import Desktop, ROOT


def hypr_json(command):
    result = subprocess.run(['hyprctl', '-j', command], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


class WindowFrame:
    def __init__(self, client, reference):
        self.address = client['address']
        if not re.fullmatch(r'0x[0-9a-fA-F]+', self.address):
            raise RuntimeError('Dirección de ventana DMO inválida')
        self.origin, self.size = tuple(client['at']), tuple(client['size'])
        self.reference = tuple(reference)
        if (len(self.origin) != 2 or len(self.size) != 2 or
                any(type(n) is not int for n in self.origin + self.size + self.reference) or
                any(n <= 0 for n in self.size + self.reference)):
            raise RuntimeError('Geometría de ventana DMO inválida')

    @classmethod
    def discover(cls, config):
        clients = [c for c in hypr_json('clients') if c.get('mapped') is True and
                   any(str(c.get(k, '')).lower() in ('dmo', 'dmo.exe')
                       for k in ('class', 'initialClass', 'title', 'initialTitle'))]
        if len(clients) != 1:
            raise RuntimeError('El modo ventana requiere una única ventana DMO visible')
        return cls(clients[0], (config['screen']['width'], config['screen']['height']))

    def focus(self):
        subprocess.run(['hyprctl', 'dispatch',
            f'hl.dsp.focus({{ window = "address:{self.address}" }})'],
            capture_output=True, text=True, check=True)
        time.sleep(0.15)
        self.validate()

    def validate(self):
        active = hypr_json('activewindow')
        if active.get('address') != self.address:
            raise RuntimeError('DMO perdió el foco; volver al juego antes de ejecutar el bot')
        if tuple(active.get('at', ())) != self.origin or tuple(active.get('size', ())) != self.size:
            raise RuntimeError('La ventana DMO cambió de posición o tamaño; reiniciar el lanzador')

    def to_global(self, point):
        return tuple(round(self.origin[i] + point[i] * self.size[i] / self.reference[i]) for i in (0, 1))

    def to_local(self, point):
        return tuple(round((point[i] - self.origin[i]) * self.reference[i] / self.size[i]) for i in (0, 1))

    @property
    def geometry(self):
        return f'{self.origin[0]},{self.origin[1]} {self.size[0]}x{self.size[1]}'


class WindowInput:
    def __init__(self, frame):
        self.frame, self.raw = frame, Desktop()

    def position(self):
        self.frame.validate()
        return self.frame.to_local(self.raw.position())

    def move(self, point):
        self.frame.validate()
        return self.frame.to_local(self.raw.move(self.frame.to_global(point)))

    def click(self):
        self.frame.validate()
        self.raw.click()

    def scroll_up(self, ticks):
        self.frame.validate()
        self.raw.scroll_up(ticks)

    def key(self, name):
        self.frame.validate()
        self.raw.key(name)


class WindowDesktop:
    """Adapt startup vision/UI without changing the original visual bot."""
    def __init__(self, legacy, frame):
        self.legacy, self.frame = legacy, frame
        self.input = WindowInput(frame)

    def prepare(self):
        self.frame.focus()
        print(f'Ventana DMO: {self.frame.geometry} · coordenadas relativas', flush=True)

    def require_tools(self):
        self.legacy.require_tools()

    def screenshot(self, validate_size=True):
        self.frame.validate()
        path = ROOT / 'debug' / 'window-current.png'
        path.parent.mkdir(exist_ok=True)
        subprocess.run(['grim', '-g', self.frame.geometry, str(path)],
                       capture_output=True, text=True, check=True)
        with Image.open(path) as source:
            image = source.convert('RGB').resize(self.frame.reference, Image.Resampling.LANCZOS)
        tiny = image.resize((64, 36))
        dark = sum(max(tiny.getpixel((x, y))) < 5 for y in range(36) for x in range(64))
        if dark / (64 * 36) > 0.98:
            raise RuntimeError(f'grim capturó DMO en negro; no se puede reconocer el inicio. Captura: {path}')
        return image

    def save_debug(self, label, image=None):
        return self.legacy.save_debug(label, image if image is not None else self.screenshot())

    def click(self, point, delay=None):
        self.input.move(point)
        self.input.click()
        time.sleep(self.legacy.config['timing']['click_delay'] if delay is None else delay)

    def key(self, name):
        self.frame.validate()
        self.legacy.key(name)
