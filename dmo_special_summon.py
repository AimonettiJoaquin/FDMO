"""Continue a confirmed revival/search panel without spending a normal summon."""
import re

from dmo_memory_bot import require_recent


def plain(text):
    return re.sub(r'<[^>]+>', '', str(text or '')).strip()


class SpecialSummonController:
    def __init__(self, desktop, config, state, name):
        self.desktop, self.config, self.name = desktop, config, name
        self.identity = (state.get('player'), state.get('manager'), state.get('own_id'))
        field = self.field(state)
        self.field_address = field['address']
        self.before = tuple(c.get('card', {}).get('address') if c.get('card') else None
                            for c in self.zones(field))
        self.position = 'Attack' if state.get('my_turn') is True else 'Defense'
        self.mode_clicked = self.zone_clicked = self.finished = False
        self.mode_prompt = None
        self.zone_identity = None
        self.zone_slot = None
        self.mode_attempts = self.zone_attempts = 0
        self.max_attempts = config.get('memory_action_attempts', 3)
        self.last = None
        self.stable = self.waited = 0

    def field(self, state):
        fields = [f for f in state.get('board', {}).get('fields', []) if f.get('local') is True]
        if len(fields) != 1 or state.get('board', {}).get('errors'):
            raise RuntimeError('Invocación especial: campo propio incompleto')
        return fields[0]

    @staticmethod
    def zones(field):
        visual = field.get('visual_candidates', {}).get('MonsterCards', {})
        zones = visual.get('candidates')
        if (visual.get('source') != 'MonsterVisualHelpers' or not isinstance(zones, list) or
                len(zones) != 5 or any(c.get('helper_index') != i for i, c in enumerate(zones))):
            raise RuntimeError('Invocación especial: zonas propias incompletas')
        return zones

    def click(self, state, point):
        width, height = self.config['screen']['width'], self.config['screen']['height']
        if len(point) != 2 or not all(type(v) is int for v in point) or not (
                0 <= point[0] < width and 0 <= point[1] < height):
            raise RuntimeError('Invocación especial: punto fuera de pantalla')
        require_recent(state)
        actual = self.desktop.move(tuple(point))
        if self.desktop.position() != actual:
            raise RuntimeError('El cursor cambió antes del clic de invocación especial')
        self.desktop.click()

    def observe(self, state):
        if self.finished:
            return None
        self.waited += 1
        if (state.get('status') != 'duel' or state.get('local_player') is not True or
                (state.get('player'), state.get('manager'), state.get('own_id')) != self.identity):
            raise RuntimeError('Invocación especial: cambió la identidad del duelo')
        field = self.field(state)
        if field.get('address') != self.field_address:
            raise RuntimeError('Invocación especial: cambió el campo propio')
        zones = self.zones(field)
        added = [c['card'] for i, c in enumerate(zones) if c.get('card') and
                 c['card'].get('name') == self.name and c['card'].get('location') == 'Field' and
                 self.before[i] is None and c['card'].get('address') not in (None, '0x0')]
        if len(added) == 1:
            self.finished = True
            return 'special_summon_verified: ' + self.name
        if self.waited >= 60:
            raise RuntimeError('Invocación especial pendiente: no se confirmó posición o colocación de ' + self.name)
        actions = state.get('actions', {})
        flags, prompts = actions.get('flags', {}), actions.get('prompts', {})
        details = actions.get('prompt_details', {})
        choice = details.get('CurrentChoiceHelper', {})
        if (actions.get('status') != 'observed' or flags.get('GameStateOpen') is not False or
                any(flags.get(k) is not False for k in ('Busy', 'SelectingCard', 'ShowChainPrompt')) or
                prompts.get('CurrentOptionHelper') != '0x0' or
                prompts.get('CurrentChoiceHelper') in (None, '0x0') or
                choice.get('Min') != 0 or choice.get('Max') != 0 or
                choice.get('UseConfirmations') is not False or
                choice.get('ChoiceType') != {'enum_raw': 0} or
                choice.get('PurposeType') != {'enum_raw': 0}):
            self.last, self.stable = None, 0
            return 'waiting_special_summon'
        text = plain(choice.get('Description'))
        mode = (text == f'Choose a Position\n({self.name})' and choice.get('Cancelable') is False and
                prompts.get('CurrentConfirmationHelper') not in (None, '0x0') and
                details.get('CurrentConfirmationHelper', {}).get('Title') == 'Mode' and
                details.get('CurrentConfirmationHelper', {}).get('ConfirmationType') == {'enum_raw': 0})
        # Spear Cretin keeps "Choose a Position" on screen when the Mode
        # control closes and a different ChoiceHelper lights up vacant zones.
        position_zone = (text == f'Choose a Position\n({self.name})' and
                         choice.get('Cancelable') is False and
                         prompts.get('CurrentChoiceHelper') != self.mode_prompt)
        placement = (self.mode_clicked and
                     (text == f'Special Summon\n{self.name}' or position_zone) and
                     prompts.get('CurrentConfirmationHelper') == '0x0')
        key = (prompts.get('CurrentChoiceHelper'), prompts.get('CurrentConfirmationHelper'), text,
               tuple(c.get('card', {}).get('address') if c.get('card') else None for c in zones))
        self.stable = self.stable + 1 if key == self.last else 1
        self.last = key
        if self.stable < 2:
            return 'checking_special_summon'
        if mode and (not self.mode_clicked or
                     prompts.get('CurrentChoiceHelper') == self.mode_prompt and
                     self.mode_attempts < self.max_attempts and self.stable >= 8):
            width, height = self.config['screen']['width'], self.config['screen']['height']
            x = 905 if self.position == 'Attack' else 1016
            self.click(state, (round(x * width / 1920), round(568 * height / 1080)))
            self.mode_clicked, self.waited = True, 0
            self.mode_prompt = prompts['CurrentChoiceHelper']
            self.mode_attempts += 1
            self.stable = 0
            return ('special_position_clicked: ' + self.position if self.mode_attempts == 1
                    else f'special_position_retry: {self.mode_attempts}/{self.max_attempts}')
        if placement and (not self.zone_clicked or key == self.zone_identity and
                          self.zone_attempts < self.max_attempts and self.stable >= 8):
            free = [i for i, c in enumerate(zones) if self.before[i] is None and
                    c.get('helper_present') is False and c.get('helper_address') == '0x0' and
                    c.get('card') is None]
            if not free:
                raise RuntimeError('Invocación especial: no hay una zona libre verificada')
            slot = min(free, key=lambda i: (abs(i - 2), i))
            if self.zone_clicked and slot != self.zone_slot:
                raise RuntimeError('Invocación especial: cambió la zona durante el reintento')
            self.click(state, tuple(self.config['zones']['own_monsters'][slot]))
            self.zone_clicked, self.waited = True, 0
            self.zone_identity, self.zone_slot = key, slot
            self.zone_attempts += 1
            self.stable = 0
            return ('special_zone_clicked' if self.zone_attempts == 1
                    else f'special_zone_retry: {self.zone_attempts}/{self.max_attempts}')
        return 'waiting_special_summon'
