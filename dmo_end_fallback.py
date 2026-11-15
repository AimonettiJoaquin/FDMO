"""Read the current phase, retry DRAW, then END and finally forfeit."""
from dmo_memory_bot import hand_signature, require_recent


class EndFallback:
    def __init__(self, desktop, config, state):
        self.desktop, self.config = desktop, config
        self.identity = (state['player'], state['manager'], state['own_id'])
        self.stage = 'inspect'
        self.last = None
        self.stable = self.waited = self.total = self.attempts = 0
        self.max_attempts = config.get('memory_action_attempts', 3)
        self.draw_signature = None
        self.draw_attempts = 0
        self.end_phase = None
        self.finished = False
        self.resume = False
        self.resumed_from_draw = False
        self.mandatory_samples = 0

    def click(self, state, point):
        point = tuple(point)
        width, height = self.config['screen']['width'], self.config['screen']['height']
        if len(point) != 2 or not all(type(v) is int for v in point) or not (
                0 <= point[0] < width and 0 <= point[1] < height):
            raise RuntimeError('Último recurso: punto fuera de pantalla')
        require_recent(state)
        actual = self.desktop.move(point)
        if self.desktop.position() != actual:
            raise RuntimeError('El cursor cambió antes del clic de último recurso')
        self.desktop.click()

    def begin_forfeit(self, state):
        require_recent(state)
        self.desktop.key('backspace')
        self.stage, self.waited, self.stable = 'forfeit_dialog', 0, 0
        self.total = 0
        return 'fallback_forfeit_requested'

    def observe(self, state):
        self.total += 1
        if (state.get('status') != 'duel' or state.get('local_player') is not True or
                (state.get('player'), state.get('manager'), state.get('own_id')) != self.identity):
            raise RuntimeError('Último recurso: no se puede verificar el duelo y jugador')
        # Once requested, finish this dialog even if the turn changes. Duel
        # exit is verified by DuelLoop; a click never marks the match finished.
        if self.stage == 'forfeit_dialog':
            self.waited += 1
            if self.waited < 4:
                return 'fallback_waiting_forfeit_dialog'
            width, height = self.config['screen']['width'], self.config['screen']['height']
            point = self.config.get('memory_forfeit_confirm_point',
                (round(1125 * width / 1920), round(633 * height / 1080)))
            self.click(state, point)
            self.stage, self.waited = 'forfeit_result', 0
            return 'fallback_forfeit_confirm_clicked'
        if self.stage == 'forfeit_result':
            self.waited += 1
            if self.waited >= 80:
                raise RuntimeError('Último recurso agotado: Forfeit no confirmó la salida del duelo')
            return 'fallback_waiting_duel_exit'
        if state.get('my_turn') is False:
            self.finished = True
            return 'turn_passed'
        if (state.get('my_turn') is True and state.get('phase') == 'DRAW' and
                self.stage in ('result', 'wait_rival') and
                self.end_phase not in (None, 'DRAW')):
            # The rival's short turn can be missed by the monitor. A new
            # DRAW after our END click confirms the preceding turn ended.
            self.finished = True
            return 'turn_passed'
        actions = state.get('actions', {})
        prompts = actions.get('prompts', {})
        details = actions.get('prompt_details', {}).get('CurrentChoiceHelper', {})
        if (actions.get('status') == 'observed' and
                prompts.get('CurrentChoiceHelper') not in (None, '0x0') and
                details.get('Cancelable') is False):
            self.mandatory_samples += 1
            if self.mandatory_samples >= 40:
                raise RuntimeError('Último recurso detenido: sigue abierta una selección obligatoria; '
                                   'no se abandona la partida por este fallo de interfaz')
            return 'fallback_waiting_mandatory_choice'
        self.mandatory_samples = 0
        if self.total >= 160:
            return self.begin_forfeit(state)
        if state.get('my_turn') is not True or state.get('phase') not in (
                'DRAW', 'STANDBY', 'MAIN 1', 'MAIN 2', 'BATTLE', 'END'):
            return 'fallback_reading_phase'
        if state['phase'] == 'END':
            self.stage = 'wait_rival'
            return 'fallback_end_verified'
        actions = state.get('actions', {})
        flags, prompts = actions.get('flags', {}), actions.get('prompts', {})
        if (actions.get('status') != 'observed' or
                any(flags.get(k) is not False for k in ('Busy', 'SelectingCard', 'ShowChainPrompt'))):
            return 'fallback_waiting_animation'
        key = (state['phase'], tuple(prompts.get(k) for k in (
            'CurrentChoiceHelper', 'CurrentOptionHelper', 'CurrentConfirmationHelper')))
        if state['phase'] == 'DRAW':
            own = [f for f in state.get('board', {}).get('fields', []) if f.get('local') is True]
            if len(own) == 1:
                key += (own[0].get('DeckCount'), own[0].get('HandCardCount'),
                        tuple((c.get('address'), c.get('name')) for c in own[0].get('HandCards', []) or []))
        if key != self.last:
            self.stable = self.waited = 0
        self.stable += 1
        self.last = key
        if self.stable < 2:
            return 'fallback_reading_phase'
        self.waited += 1
        free = all(prompts.get(k) == '0x0' for k in (
            'CurrentChoiceHelper', 'CurrentOptionHelper', 'CurrentConfirmationHelper'))
        if state['phase'] == 'DRAW' and free:
            self.resumed_from_draw = True
            try:
                field = next(f for f in state['board']['fields'] if f.get('local') is True)
                deck = field.get('DeckCount')
                signature = hand_signature(state) + (deck,)
                cards = field['HandCards']
                if (type(deck) is not int or deck <= 0 or
                        any(c.get('address') in (None, '0x0') for c in cards) or
                        len({c['address'] for c in cards}) != len(cards)):
                    signature = None
            except (KeyError, TypeError, ValueError, StopIteration):
                signature = None
            changed = self.draw_signature is not None and signature != self.draw_signature
            if (flags.get('DrewForTurn') is True or changed) and self.waited < 24:
                return 'fallback_waiting_draw_exit'
            if (signature is not None and flags.get('DrewForTurn') is False and
                    not changed and
                    self.draw_attempts < self.max_attempts and
                    (self.draw_attempts == 0 or self.waited >= 12)):
                self.click(state, self.config['points']['deck'])
                self.draw_signature = signature
                self.draw_attempts += 1
                self.stage, self.waited = 'draw', 0
                return f'fallback_draw_clicked: {self.draw_attempts}/{self.max_attempts}'
            if self.stage == 'draw' and self.waited < 24:
                return 'fallback_waiting_draw'
        if state['phase'] == 'STANDBY' and free and self.waited < 12:
            self.resumed_from_draw = True
            return 'fallback_waiting_standby'
        if self.resumed_from_draw and state['phase'] in ('MAIN 1', 'MAIN 2'):
            self.finished = self.resume = True
            return 'fallback_phase_recovered: ' + state['phase']
        # A fresh phase supersedes the old END attempt. DRAW above gets its
        # own retry budget even if END was being attempted on the prior turn.
        if self.end_phase is not None and self.end_phase != state['phase']:
            self.stage, self.waited = 'inspect', 0
            self.end_phase = state['phase']
        if self.stage in ('inspect', 'draw', 'wait_rival'):
            return self.open_menu(state)
        if self.stage == 'menu':
            if self.waited < 2:
                return 'fallback_waiting_menu'
            self.click(state, self.config['points']['phase_end'])
            self.stage, self.waited = 'result', 0
            return f'fallback_end_clicked: {self.attempts}/{self.max_attempts}'
        if self.stage == 'result':
            if self.waited >= 16:
                if self.attempts >= self.max_attempts:
                    return self.begin_forfeit(state)
                return self.open_menu(state)
            return 'fallback_waiting_end'
        raise RuntimeError('Etapa de último recurso desconocida')

    def open_menu(self, state):
        if self.attempts >= self.max_attempts:
            return self.begin_forfeit(state)
        require_recent(state)
        self.desktop.key('tab')
        self.attempts += 1
        self.end_phase = state['phase']
        self.stage, self.waited = 'menu', 0
        return f'fallback_end_menu: {self.attempts}/{self.max_attempts}'
