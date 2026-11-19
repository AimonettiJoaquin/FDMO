#!/usr/bin/env python3
"""Bot visual, conservador y compatible con Wayland para DMO.

No lee memoria, no inyecta DLLs ni modifica el cliente: únicamente captura la
pantalla mediante grim y emite los mismos eventos de teclado/ratón que un
usuario mediante ydotool.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image


from dmo.paths import ROOT
CONFIG_PATH = ROOT / "config.json"
TEMPLATE_DIR = ROOT / "templates"
DEBUG_DIR = ROOT / "debug"


DEFAULT_CONFIG = {
    "screen": {"width": 1280, "height": 720},
    "timing": {"click_delay": 0.8, "animation_delay": 2.5, "transition_timeout": 12, "poll_interval": 0.5, "calibration_switch_delay": 3, "duel_end_no_phase_timeout": 30, "phase_change_retries": 3, "recovery_attempts": 3},
    "recognition": {
        "threshold": 0.13,
        "phase_yellow_threshold": 0.18,
        "opponent_red_threshold": 0.18,
        "attack_target_white_threshold": 0.2,
        "world_hud_threshold": 0.25,
        "regions": {
            # Minimap region; recognition uses its central green player arrow.
            "world": [1050, 0, 230, 200],
            "duel_setup": [840, 395, 420, 315],
            "duel_lobby": [300, 90, 700, 550],
            # Phase picker includes the yellow YOU / red enemy name and phase.
            "my_draw": [0, 50, 900, 250],
            "my_main": [350, 80, 580, 160],
            "my_battle": [350, 80, 580, 160],
            "enemy_turn": [350, 80, 580, 160],
            "attack_target": [450, 10, 380, 75],
        },
    },
    "points": {
        "add_npc": [615, 555],
        "bonz": [755, 300],
        "ready": [840, 570],
        "start": [1030, 640],
        "deck": [640, 390],
        "hand_first": [285, 670],
        "summon": [110, 535],
        "summon_slot": [640, 500],
        "phase_battle": [705, 250],
        "phase_main": [575, 250],
        "phase_end": [930, 250],
        "attack_button": [640, 470],
        "direct_attack": [640, 105],
    },
    "zones": {
        "own_monsters": [[445, 485], [545, 485], [640, 485], [735, 485], [835, 485]],
        "opponent_monsters": [[445, 240], [545, 240], [640, 240], [735, 240], [835, 240]],
    },
    "attack_sword_offset": [0, -120],
    "target_confirm_offset": [0, 105],
    "max_attack_actions": 12,
}

POINT_HINTS = {
    "start": "abre Duel Settings con Z y apunta al botón Start",
    "add_npc": "abre el lobby con Z y apunta al botón '+' / Add NPC",
    "bonz": "abre la lista de NPC y apunta al retrato de Bonz",
    "ready": "apunta al botón Ready",
    "deck": "ya dentro del duelo, apunta al mazo central para robar",
    "hand_first": "en tu Main Phase, apunta a la primera carta de tu mano",
    "summon": "selecciona una carta de la mano y apunta a la opción Summon",
    "summon_slot": "después de Summon, apunta a una zona libre de monstruos propia",
    "phase_battle": "pulsa TAB y apunta al botón Battle de selector de fases",
    "phase_end": "pulsa TAB y apunta al botón End de selector de fases",
    "direct_attack": "en selector de objetivo, apunta al objetivo de ataque directo / rival",
}

STATE_HINTS = {
    "world": "mundo normal, fuera de cualquier menú o duelo",
    "duel_setup": "Duel Settings abierto después de pulsar Z",
    "duel_lobby": "lobby de duelo abierto después de pulsar Z y antes de Ready",
    "my_main": "tu turno en Main Phase 1, con el selector de fases abierto al pulsar Tab",
    "my_draw": "tu turno en Draw Phase, con el selector de fases abierto al pulsar Tab",
    "my_battle": "tu turno en Battle Phase, con el selector de fases abierto al pulsar Tab",
    "enemy_turn": "turno de Bonz, con Bonz rojo visible y el selector de fases abierto al pulsar Tab",
    "attack_target": "mensaje 'Choose an attack target' visible",
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
        print(f"Creado {CONFIG_PATH.name}. Ejecuta primero: python dmo_bot.py calibrate")
        return DEFAULT_CONFIG
    config = json.loads(CONFIG_PATH.read_text())
    # Preserve user calibration while adding keys introduced by newer versions.
    def add_missing(current: dict, defaults: dict) -> None:
        for key, value in defaults.items():
            if key not in current:
                current[key] = value
            elif isinstance(value, dict) and isinstance(current[key], dict):
                add_missing(current[key], value)

    add_missing(config, DEFAULT_CONFIG)
    return config


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")


def switch_to_game_delay(config: dict) -> None:
    """Lets a terminal-driven calibration capture a game in another workspace."""
    seconds = config["timing"].get("calibration_switch_delay", 3)
    input(
        f"Cuando estés listo, pulsa Enter y vuelve al workspace de DMO: "
        f"la captura será en {seconds} segundos... "
    )
    for remaining in range(seconds, 0, -1):
        print(f"  capturando en {remaining}...", end="\r", flush=True)
        time.sleep(1)
    print(" " * 40, end="\r")


class SafetyStop(RuntimeError):
    pass


class Desktop:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.width = config["screen"]["width"]
        self.height = config["screen"]["height"]

    def require_tools(self) -> None:
        missing = [tool for tool in ("grim", "ydotool") if not shutil.which(tool)]
        if missing:
            raise SafetyStop(
                "Faltan herramientas: " + ", ".join(missing) + ". "
                "En Arch instala ydotool y habilita ydotoold antes de ejecutar el bot."
            )

    def screenshot(self, validate_size: bool = True) -> Image.Image:
        path = DEBUG_DIR / "current.png"
        DEBUG_DIR.mkdir(exist_ok=True)
        result = subprocess.run(["grim", str(path)], capture_output=True, text=True)
        if result.returncode:
            raise SafetyStop(f"grim falló: {result.stderr.strip()}")
        image = Image.open(path).convert("RGB")
        if validate_size and image.size != (self.width, self.height):
            raise SafetyStop(
                f"Resolución capturada {image.size}; se requiere {(self.width, self.height)}. "
                "Mantén la resolución usada al calibrar o ejecuta calibrate nuevamente."
            )
        return image

    def save_debug(self, label: str, image: Image.Image | None = None) -> Path:
        DEBUG_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = DEBUG_DIR / f"{stamp}-{label}.png"
        (image or self.screenshot()).save(path)
        return path

    def click(self, point: list[int] | tuple[int, int], delay: float | None = None) -> None:
        x, y = map(int, point)
        # ydotool's absolute motion is unreliable on scaled Wayland outputs.
        # Hyprland moves its own cursor in the same coordinate system reported
        # by `hyprctl cursorpos`, which is also what calibration records.
        # Hyprland's Lua API is used instead of the removed legacy dispatcher.
        # `hyprctl dispatch` performs hl.dispatch(...) itself.
        lua = f"hl.dsp.cursor.move({{ x = {x}, y = {y} }})"
        subprocess.run(["hyprctl", "dispatch", lua], check=True)
        subprocess.run(["ydotool", "click", "0xC0"], check=True)
        time.sleep(self.config["timing"]["click_delay"] if delay is None else delay)

    def key(self, name: str) -> None:
        # Linux evdev key codes: TAB=15, Z=44.
        codes = {"tab": 15, "z": 44}
        code = codes[name]
        subprocess.run(["ydotool", "key", f"{code}:1", f"{code}:0"], check=True)
        time.sleep(self.config["timing"]["click_delay"])


class Vision:
    def __init__(self, desktop: Desktop, config: dict) -> None:
        self.desktop = desktop
        self.config = config

    def _template_path(self, state: str) -> Path:
        return TEMPLATE_DIR / f"{state}.png"

    def calibrated(self, state: str) -> bool:
        return self._template_path(state).exists()

    def score(self, state: str, image: Image.Image | None = None) -> float:
        path = self._template_path(state)
        if not path.exists():
            return 1.0
        x, y, w, h = self.config["recognition"]["regions"][state]
        frame = np.asarray((image or self.desktop.screenshot()).crop((x, y, x + w, y + h)), dtype=np.int16)
        template = np.asarray(Image.open(path).convert("RGB"), dtype=np.int16)
        if frame.shape != template.shape:
            return 1.0
        if state == "world":
            return self._world_hud_score(frame, template)
        if state.startswith("my_"):
            # The phase picker is translucent: scenery behind it changes every
            # frame. Compare only its invariant yellow pixels (YOU + selected
            # phase ring) rather than opaque RGB background.
            template_yellow = self._yellow_mask(template)
            frame_yellow = self._yellow_mask(frame)
            expected = int(template_yellow.sum())
            if expected == 0:
                return 1.0
            missing = np.logical_and(template_yellow, np.logical_not(frame_yellow)).sum()
            unexpected = np.logical_and(frame_yellow, np.logical_not(template_yellow)).sum()
            return float((missing + unexpected * 0.25) / expected)
        if state == "enemy_turn":
            template_red = self._red_mask(template)
            frame_red = self._red_mask(frame)
            expected = int(template_red.sum())
            if expected == 0:
                return 1.0
            missing = np.logical_and(template_red, np.logical_not(frame_red)).sum()
            unexpected = np.logical_and(frame_red, np.logical_not(template_red)).sum()
            return float((missing + unexpected * 0.15) / expected)
        if state == "attack_target":
            template_white = self._white_mask(template)
            frame_white = self._white_mask(frame)
            expected = int(template_white.sum())
            if expected == 0:
                return 1.0
            missing = np.logical_and(template_white, np.logical_not(frame_white)).sum()
            unexpected = np.logical_and(frame_white, np.logical_not(template_white)).sum()
            return float((missing + unexpected * 0.15) / expected)
        return float(np.abs(frame - template).mean() / 255.0)

    @classmethod
    def _world_hud_score(cls, frame: np.ndarray, template: np.ndarray) -> float:
        """Match the fixed green player arrow at the minimap's center.

        Compare its color mask and silhouette with the calibrated reference,
        ignoring terrain, lighting and the compass N that moves with rotation.
        """
        height, width = template.shape[:2]
        left, right = round(width * 0.45), round(width * 0.75)
        top, bottom = round(height * 0.35), round(height * 0.7)
        expected = cls._green_mask(template[top:bottom, left:right])
        actual = cls._green_mask(frame[top:bottom, left:right])
        count = int(expected.sum())
        if count < 3:
            return 1.0  # no usable player arrow in this calibration
        # Penalize extra green pixels too: a green patch is not an arrow.
        return float(np.logical_xor(expected, actual).sum() / count)

    @staticmethod
    def _green_mask(image: np.ndarray) -> np.ndarray:
        red, green, blue = np.moveaxis(image.astype(np.int16), -1, 0)
        return (green > 180) & (red < 180) & (blue < 120) & ((green - red) > 70) & ((green - blue) > 100)

    @staticmethod
    def _yellow_mask(image: np.ndarray) -> np.ndarray:
        red, green, blue = image[..., 0], image[..., 1], image[..., 2]
        return (red > 165) & (green > 145) & (blue < 125) & ((red.astype(np.int16) + green.astype(np.int16) - 2 * blue.astype(np.int16)) > 170)

    @staticmethod
    def _red_mask(image: np.ndarray) -> np.ndarray:
        red, green, blue = image[..., 0], image[..., 1], image[..., 2]
        return (red > 165) & (green < 125) & (blue < 125) & ((red.astype(np.int16) - green.astype(np.int16)) > 90) & ((red.astype(np.int16) - blue.astype(np.int16)) > 70)

    @staticmethod
    def _white_mask(image: np.ndarray) -> np.ndarray:
        red, green, blue = image[..., 0], image[..., 1], image[..., 2]
        return (red > 190) & (green > 190) & (blue > 190)

    def is_state(self, state: str, image: Image.Image | None = None) -> bool:
        if state.startswith("my_"):
            threshold = self.config["recognition"]["phase_yellow_threshold"]
        elif state == "enemy_turn":
            threshold = self.config["recognition"]["opponent_red_threshold"]
        elif state == "attack_target":
            threshold = self.config["recognition"]["attack_target_white_threshold"]
        elif state == "world":
            threshold = self.config["recognition"].get("world_hud_threshold", 0.25)
        else:
            threshold = self.config["recognition"]["threshold"]
        return self.score(state, image) <= threshold

    def wait_for(self, state: str, timeout: float | None = None) -> None:
        if not self.calibrated(state):
            raise SafetyStop(f"Falta la plantilla de estado '{state}'. Ejecuta calibrate.")
        timeout = timeout or self.config["timing"]["transition_timeout"]
        deadline = time.monotonic() + timeout
        best = 1.0
        while time.monotonic() < deadline:
            image = self.desktop.screenshot()
            best = min(best, self.score(state, image))
            if self.is_state(state, image):
                return
            time.sleep(self.config["timing"]["poll_interval"])
        path = self.desktop.save_debug(f"timeout-{state}")
        raise SafetyStop(f"No apareció '{state}' (mejor diferencia: {best:.3f}). Captura: {path}")

    def wait_for_any(self, states: tuple[str, ...], timeout: float | None = None) -> str:
        """Wait for one of several valid UI states and return the matching one."""
        for state in states:
            if not self.calibrated(state):
                raise SafetyStop(f"Falta la plantilla de estado '{state}'. Ejecuta calibrate.")
        timeout = timeout or self.config["timing"]["transition_timeout"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            image = self.desktop.screenshot()
            for state in states:
                if self.is_state(state, image):
                    return state
            time.sleep(self.config["timing"]["poll_interval"])
        path = self.desktop.save_debug("timeout-expected-duel-screen")
        raise SafetyStop(f"No apareció ninguno de estos estados: {', '.join(states)}. Captura: {path}")


class Bot:
    def __init__(self, config: dict) -> None:
        self.config = config
        self.desktop = Desktop(config)
        self.vision = Vision(self.desktop, config)
        self.points = config["points"]
        zones = config["zones"]["own_monsters"]
        center = len(zones) // 2
        # Prefer the center, then expand outward: 3, 2, 4, 1, 5.
        indices = [center, center - 1, center + 1, center - 2, center + 2]
        self.summon_slots = [zones[index] for index in indices if 0 <= index < len(zones)]

    def click(self, name: str) -> None:
        self.desktop.click(self.points[name])

    def settle(self, seconds: float | None = None) -> None:
        """Wait for non-skippable game animations before the next action."""
        time.sleep(seconds if seconds is not None else self.config["timing"].get("animation_delay", 2.5))

    def detect_phase(self, image: Image.Image) -> str | None:
        for state in ("my_draw", "my_main", "my_battle", "enemy_turn"):
            if self.vision.is_state(state, image):
                return state
        return None

    def open_phase_selector(self) -> str | None:
        """Check both toggle positions; leave a recognized selector open."""
        phase = self.detect_phase(self.desktop.screenshot())
        if phase:
            return phase
        self.desktop.key("tab")
        self.settle(0.7)
        phase = self.detect_phase(self.desktop.screenshot())
        if not phase:
            self.desktop.key("tab")  # restore the original toggle position
        return phase

    def wait_for_own_phase(self, timeout: float = 90) -> str | None:
        """Return draw/main/battle only when the yellow YOU phase panel is visible."""
        deadline = time.monotonic() + timeout
        no_phase_since = time.monotonic()
        while time.monotonic() < deadline:
            phase = self.open_phase_selector()
            if phase:
                print(f"Fase detectada: {phase.removeprefix('my_').replace('enemy_turn', 'BONZ').upper()}.")
                return phase  # caller closes the selector when it is ready to act
            if time.monotonic() - no_phase_since >= self.config["timing"].get("duel_end_no_phase_timeout", 25):
                recovered = self.recover_stalled_phase()
                if recovered:
                    return recovered
                image = self.desktop.screenshot()
                phase = self.detect_phase(image)
                if phase:
                    return phase
                if self.vision.is_state("world", image):
                    print("Sin fases y regreso al mundo confirmado; duelo terminado.")
                    return None
                path = self.desktop.save_debug("phase-recovery-exhausted", image)
                raise SafetyStop(
                    "No se pudo recuperar la fase ni confirmar el regreso al mundo. "
                    f"No se iniciará otra partida. Captura: {path}"
                )
            time.sleep(1)
        path = self.desktop.save_debug("timeout-phase")
        raise SafetyStop(f"No apareció un turno propio dentro de {timeout} s. Captura: {path}")

    def recover_stalled_phase(self) -> str | None:
        """Probe the phase selector repeatedly before abandoning an active duel."""
        attempts = self.config["timing"].get("recovery_attempts", 3)
        for attempt in range(1, attempts + 1):
            print(f"Recuperación de fase: intento {attempt}/{attempts}.")
            phase = self.open_phase_selector()
            if phase:
                print(f"Recuperación: {phase.removeprefix('my_').replace('enemy_turn', 'BONZ').upper()} confirmado.")
                return phase
            self.settle(0.5)
        return None

    def start_duel(self) -> None:
        self.desktop.key("z")
        state = self.vision.wait_for_any(("duel_setup", "duel_lobby"))
        if state == "duel_setup":
            self.click("start")
            self.settle()
            self.vision.wait_for("duel_lobby")
        for name in ("add_npc", "bonz", "ready"):
            self.click(name)
            self.settle(1.5)
        # From here on, run() waits through the Tab-based phase state machine.

    def summon_once(self) -> bool:
        # Deck is restricted to level 4 or lower. The action is still checked by
        # the game UI, so an unavailable summon cannot silently advance the bot.
        self.click("hand_first")
        self.settle(1)
        self.click("summon")
        self.settle(1)
        print("Invocación: probar todas las zonas de monstruos.")
        for slot in self.summon_slots:
            # The game accepts the first free highlighted slot and ignores the
            # rest. Fast probing handles a center monster being destroyed.
            self.desktop.click(slot, delay=self.config["timing"].get("summon_slot_delay", 0.15))
        self.settle()
        return True

    def draw_card(self) -> None:
        self.click("deck")
        self.settle()

    def go_to_battle(self) -> bool:
        """Select Battle and confirm it, retrying if the UI ignored the click."""
        attempts = self.config["timing"].get("phase_change_retries", 3)
        print("Cambio solicitado: MAIN → BATTLE.")
        for attempt in range(1, attempts + 1):
            phase = self.open_phase_selector()
            if phase == "my_battle":
                self.desktop.key("tab")
                return True
            if phase != "my_main":
                if phase:
                    self.desktop.key("tab")
                return False
            self.click("phase_battle")
            self.settle()

            # A disabled Battle button can leave the picker open. Inspect it
            # before toggling, or every subsequent check will see a closed UI.
            phase = self.open_phase_selector()
            if phase:
                self.desktop.key("tab")
            if phase == "my_battle":
                print("Cambio MAIN → BATTLE confirmado.")
                return True
            print(f"MAIN → BATTLE no confirmado; reintentar ({attempt}/{attempts}).")
            self.settle(0.5)
        print("No se pudo confirmar BATTLE; intentar END si continúa el turno propio.")
        return False

    def attack_all(self) -> None:
        """Attack, then restart the scan to catch delayed/multi-attack monsters."""
        sword_dx, sword_dy = self.config["attack_sword_offset"]
        confirm_dx, confirm_dy = self.config["target_confirm_offset"]
        attacks = 0
        while attacks < self.config.get("max_attack_actions", 12):
            resolved = False
            for zone in self.config["zones"]["own_monsters"]:
                # Fast probe: empty/already-used zones will not show the prompt.
                self.desktop.click(zone, delay=self.config["timing"].get("attack_probe_delay", 0.3))
                self.desktop.click((zone[0] + sword_dx, zone[1] + sword_dy), delay=self.config["timing"].get("attack_probe_delay", 0.3))
                deadline = time.monotonic() + self.config["timing"].get("attack_target_timeout", 1.5)
                while time.monotonic() < deadline and not self.attack_target_visible():
                    time.sleep(0.15)
                if not self.attack_target_visible():
                    continue
                print(f"Objetivo de ataque confirmado (diferencia {self.vision.score('attack_target'):.3f}).")

                # Brute-force legal enemy locations. The game ignores empty
                # zones; a successful confirmation closes the target prompt.
                for target in self.config["zones"]["opponent_monsters"]:
                    target_delay = self.config["timing"].get("targeting_delay", 0.3)
                    self.desktop.click(target, delay=target_delay)
                    self.desktop.click((target[0] + confirm_dx, target[1] + confirm_dy), delay=target_delay)
                    if not self.vision.is_state("attack_target"):
                        resolved = True
                        self.settle()  # actual attack animation completed here
                        break
                    time.sleep(self.config["timing"].get("target_cycle_delay", 0.15))
                if not resolved and self.attack_target_visible():
                    self.desktop.click(self.points["direct_attack"])
                    self.settle()
                    resolved = not self.attack_target_visible()
                if resolved:
                    attacks += 1
                    print(f"Ataque {attacks}: resuelto; reiniciar escaneo de monstruos.")
                    break
            if not resolved:
                print("No quedan ataques disponibles en esta Battle Phase.")
                return
        print("Límite de ataques alcanzado; finalizar Battle Phase.")

    def attack_target_visible(self) -> bool:
        """Require two stable frames before clicking any enemy target."""
        if not self.vision.is_state("attack_target"):
            return False
        time.sleep(0.12)
        return self.vision.is_state("attack_target")

    def end_turn(self) -> bool:
        phase = self.open_phase_selector()
        if phase not in ("my_main", "my_battle"):
            if phase:
                self.desktop.key("tab")
            print("END pendiente: no se confirmó MAIN/BATTLE propia.")
            return False
        print("Cambio solicitado: END.")
        self.click("phase_end")
        self.settle()
        return True

    def run(self, matches: int) -> None:
        self.desktop.require_tools()
        print("Deja DMO en el mundo, fuera de menús o duelos.")
        switch_to_game_delay(self.config)
        self.desktop.screenshot()  # validates dimensions before sending input
        match = 1
        while matches == 0 or match <= matches:
            total = "∞" if matches == 0 else str(matches)
            print(f"Partida {match}/{total}")
            self.start_duel()
            opening_own_turn = True
            opponent_started = False
            summoned_this_turn = False
            while True:
                phase = self.wait_for_own_phase()
                if phase is None:
                    print("Partida terminada; volver al ciclo de inicio.")
                    self.settle(3)
                    break
                self.desktop.key("tab")  # close the detected phase selector
                if phase == "enemy_turn":
                    print("Turno de Bonz: esperar sin actuar.")
                    if opening_own_turn:
                        opponent_started = True
                    self.settle(2)
                    continue
                if phase == "my_draw":
                    print("Acción: robar carta.")
                    self.draw_card()
                    summoned_this_turn = False
                    continue
                if phase == "my_main":
                    if not summoned_this_turn:
                        print("Acción: invocar un monstruo.")
                        self.summon_once()
                        summoned_this_turn = True
                    # Draw occurs for either player in this rule set. Only a
                    # prior red Bonz panel tells us that the enemy started.
                    if opening_own_turn and not opponent_started:
                        print("Primer turno: finalizar sin atacar.")
                        if self.end_turn():
                            opening_own_turn = False
                    else:
                        if not self.go_to_battle():
                            if self.end_turn():
                                opening_own_turn = False
                    continue
                if phase == "my_battle":
                    # Battle happens only after the bot has confirmed yellow YOU.
                    print("Acción: atacar y finalizar turno.")
                    self.attack_all()
                    self.end_turn()
                    opening_own_turn = False
                    continue
            match += 1


def record_point(config: dict, name: str) -> None:
    hint = POINT_HINTS.get(name, name)
    print(f"{hint}. Pon el cursor exactamente allí.")
    switch_to_game_delay(config)
    result = subprocess.run(["hyprctl", "cursorpos"], capture_output=True, text=True, check=True)
    x, y = (int(part.strip()) for part in result.stdout.strip().split(","))
    config["points"][name] = [x, y]
    print(f"  {name}: {x}, {y}")


def scale_value(value: object, sx: float, sy: float) -> object:
    """Scale saved screen geometry when the calibration resolution changes."""
    if isinstance(value, dict):
        return {key: scale_value(item, sx, sy) for key, item in value.items()}
    if isinstance(value, list) and len(value) == 2 and all(isinstance(v, int | float) for v in value):
        return [round(value[0] * sx), round(value[1] * sy)]
    if isinstance(value, list) and len(value) == 4 and all(isinstance(v, int | float) for v in value):
        return [round(value[0] * sx), round(value[1] * sy), round(value[2] * sx), round(value[3] * sy)]
    if isinstance(value, list):
        return [scale_value(item, sx, sy) for item in value]
    return value


def calibrate(config: dict) -> None:
    desktop = Desktop(config)
    # Calibration is the one moment in which the old configured resolution is
    # intentionally ignored. This makes fullscreen 1920×1080 work out of the box.
    actual_width, actual_height = desktop.screenshot(validate_size=False).size
    # Every calibration starts from unscaled defaults. This prevents stale
    # coordinates/templates from a previous failed calibration being reused.
    delay = config.get("timing", {}).get("calibration_switch_delay", 3)
    fresh = copy.deepcopy(DEFAULT_CONFIG)
    sx, sy = actual_width / DEFAULT_CONFIG["screen"]["width"], actual_height / DEFAULT_CONFIG["screen"]["height"]
    fresh["points"] = scale_value(fresh["points"], sx, sy)
    fresh["zones"] = scale_value(fresh["zones"], sx, sy)
    fresh["recognition"]["regions"] = scale_value(fresh["recognition"]["regions"], sx, sy)
    fresh["screen"] = {"width": actual_width, "height": actual_height}
    fresh["timing"]["calibration_switch_delay"] = delay
    config.clear()
    config.update(fresh)
    desktop = Desktop(config)
    print(f"Resolución detectada: {actual_width}×{actual_height}; se reinició la calibración anterior.")
    print(f"Calibración: DMO debe estar visible a {actual_width}×{actual_height} en el monitor principal.")
    print("Para cada punto, mueve el cursor al centro del control dentro del juego y pulsa Enter aquí.")
    for name in ("start", "add_npc", "bonz", "ready", "deck", "hand_first", "summon", "summon_slot", "phase_main", "phase_battle", "phase_end", "direct_attack"):
        record_point(config, name)

    TEMPLATE_DIR.mkdir(exist_ok=True)
    print("\nAhora prepara cada pantalla indicada en DMO y pulsa Enter para guardar su referencia visual.")
    for state, rect in config["recognition"]["regions"].items():
        hint = STATE_HINTS.get(state, state)
        print(f"Prepara: {hint}.")
        switch_to_game_delay(config)
        x, y, w, h = rect
        desktop.screenshot().crop((x, y, x + w, y + h)).save(TEMPLATE_DIR / f"{state}.png")
        print(f"  guardada templates/{state}.png")
    save_config(config)
    print("Calibración terminada.")


def reset_template_regions(config: dict) -> None:
    """Restore only the visual crops for the configured screen resolution."""
    sx = config["screen"]["width"] / DEFAULT_CONFIG["screen"]["width"]
    sy = config["screen"]["height"] / DEFAULT_CONFIG["screen"]["height"]
    config["recognition"]["regions"] = scale_value(
        copy.deepcopy(DEFAULT_CONFIG["recognition"]["regions"]), sx, sy
    )


def capture_templates(config: dict) -> None:
    """Recapture state references without changing calibrated click points."""
    desktop = Desktop(config)
    desktop.screenshot()  # validates that DMO still uses the calibrated size
    reset_template_regions(config)
    TEMPLATE_DIR.mkdir(exist_ok=True)
    print("Se conservarán las coordenadas de clic existentes.")
    print("Prepara cada estado exacto en DMO; tras Enter vuelve al juego antes de la captura.")
    for state, rect in config["recognition"]["regions"].items():
        hint = STATE_HINTS.get(state, state)
        print(f"Prepara: {hint}.")
        switch_to_game_delay(config)
        x, y, w, h = rect
        desktop.screenshot().crop((x, y, x + w, y + h)).save(TEMPLATE_DIR / f"{state}.png")
        print(f"  guardada templates/{state}.png")
    save_config(config)
    print("Plantillas actualizadas.")


def capture_template(config: dict, state: str) -> None:
    """Recapture one visual state without changing any click coordinate."""
    desktop = Desktop(config)
    desktop.screenshot()
    sx = config["screen"]["width"] / DEFAULT_CONFIG["screen"]["width"]
    sy = config["screen"]["height"] / DEFAULT_CONFIG["screen"]["height"]
    config["recognition"]["regions"][state] = scale_value(
        copy.deepcopy(DEFAULT_CONFIG["recognition"]["regions"][state]), sx, sy
    )
    TEMPLATE_DIR.mkdir(exist_ok=True)
    print(f"Prepara: {STATE_HINTS.get(state, state)}.")
    switch_to_game_delay(config)
    x, y, w, h = config["recognition"]["regions"][state]
    desktop.screenshot().crop((x, y, x + w, y + h)).save(TEMPLATE_DIR / f"{state}.png")
    save_config(config)
    print(f"Plantilla '{state}' actualizada.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Bot visual de farmeo DMO (Wayland).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("calibrate", help="Guarda coordenadas y referencias visuales.")
    sub.add_parser("capture-templates", help="Recaptura estados visuales sin cambiar coordenadas.")
    one = sub.add_parser("capture-template", help="Recaptura un único estado visual.")
    one.add_argument("state", choices=tuple(DEFAULT_CONFIG["recognition"]["regions"]))
    run = sub.add_parser("run", help="Inicia el ciclo automático contra Bonz Easy.")
    run.add_argument("--matches", type=int, default=1, help="Cantidad de duelos; usa 0 para repetir indefinidamente (por defecto: 1).")
    args = parser.parse_args()
    config = load_config()
    try:
        if args.command == "calibrate":
            calibrate(config)
        elif args.command == "capture-templates":
            capture_templates(config)
        elif args.command == "capture-template":
            capture_template(config, args.state)
        else:
            Bot(config).run(args.matches)
    except (SafetyStop, subprocess.CalledProcessError) as error:
        print(f"PARADA SEGURA: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
