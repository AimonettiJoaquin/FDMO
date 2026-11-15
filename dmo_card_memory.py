"""Read-only knowledge of our set cards; never replaces raw memory readings."""
import argparse
from copy import deepcopy
from datetime import datetime
import json
import math
from pathlib import Path


class OwnCardMemory:
    def __init__(self, max_gap=3.0):
        self.max_gap = max_gap
        self.previous = None
        self.cards = {}
        self.sequence = 0

    def clear(self):
        self.previous = None
        self.cards.clear()

    @staticmethod
    def extract(state):
        fields = [f for f in state.get('board', {}).get('fields', []) if f.get('local') is True]
        if len(fields) != 1 or not state.get('local_player'):
            raise ValueError('Campo local no identificado')
        field = fields[0]
        context = (state['player'], state['manager'], field['address'],
                   field['owner_identity'], field.get('generation_id'))
        hand = {}
        for card in field['HandCards']:
            if (not card or card.get('unknown') or not card.get('name') or
                    card.get('location') != 'Hand' or card['address'] in hand):
                raise ValueError('Mano incompleta o ambigua')
            hand[card['address']] = card['name']
        if len(hand) != field['HandCardCount']:
            raise ValueError('Conteo de mano no coincide')
        slots = {}
        for zone in ('MonsterCards', 'MagicCards'):
            visual = field['visual_candidates'][zone]
            if 'error' in visual:
                raise ValueError('Colección visual sin lectura')
            entries = visual['candidates']
            if not 1 <= len(entries) <= 32:
                raise ValueError('Colección visual incompleta')
            for index, entry in enumerate(entries):
                if entry['helper_index'] != index or 'error' in entry:
                    raise ValueError('Referencia visual ilegible o desordenada')
                key = (zone, index)
                card = entry['card']
                if entry['helper_address'] == '0x0' and card is None:
                    slots[key] = None
                    continue
                if not card or entry['helper_address'] == '0x0':
                    raise ValueError('Helper sin carta')
                facedown = card.get('face_down')
                if facedown not in (True, False) or (card.get('unknown') and facedown is not True):
                    raise ValueError('Estado de carta desconocido')
                if not card.get('unknown') and (not card.get('name') or card.get('location') != 'Field'):
                    raise ValueError('Carta fuera del campo o sin nombre')
                slots[key] = (entry['helper_address'], card['address'], facedown, card.get('name'))
        return dict(context=context, hand=hand, slots=slots,
                    my_turn=state.get('my_turn'), phase=state.get('phase'))

    def observe(self, state, observed_at):
        if state.get('status') != 'duel':
            self.clear()
            return dict(status='inactive', cards=[])
        try:
            if not math.isfinite(observed_at):
                raise ValueError('Tiempo inválido')
            current = self.extract(state)
        except (KeyError, TypeError, ValueError) as error:
            self.clear()
            return dict(status='unavailable', reason=str(error), cards=[])
        current['time'] = observed_at
        before = self.previous
        continuous = (before is not None and before['context'] == current['context'] and
                      0 < observed_at - before['time'] <= self.max_gap)
        if not continuous:
            self.cards.clear()
        else:
            # Forget on reveal, removal or replacement. A gap loses continuity
            # even if an address happens to be reused after the gap.
            for key in list(self.cards):
                slot = current['slots'].get(key)
                old_slot = before['slots'].get(key)
                if not slot or slot != old_slot or slot[2] is not True or slot[3]:
                    del self.cards[key]
            removed = before['hand'].keys() - current['hand'].keys()
            added = current['hand'].keys() - before['hand'].keys()
            changed = [key for key in before['slots'].keys() | current['slots'].keys()
                       if before['slots'].get(key) != current['slots'].get(key)]
            unchanged_hand = all(before['hand'][a] == name for a, name in current['hand'].items()
                                 if a in before['hand'])
            if (len(removed) == 1 and not added and unchanged_hand and len(changed) == 1 and
                    before['my_turn'] is True and current['my_turn'] is True and
                    before['phase'] == current['phase'] and current['phase'] in ('MAIN 1', 'MAIN 2') and
                    before['slots'].keys() == current['slots'].keys()):
                key = changed[0]
                slot = current['slots'][key]
                if before['slots'][key] is None and slot and slot[2] is True and not slot[3]:
                    hand_address = next(iter(removed))
                    self.sequence += 1
                    self.cards[key] = dict(event_id=f'set-{self.sequence}', zone=key[0],
                        helper_index=key[1], name=before['hand'][hand_address],
                        source='inferred_transition', confirmed=False,
                        evidence='Una carta salió de la mano y apareció una referencia boca abajo.',
                        hand_address=hand_address, helper_address=slot[0], card_address=slot[1])
        self.previous = current
        return self.snapshot()

    def snapshot(self):
        return dict(status='tracking', cards=deepcopy(list(self.cards.values())))

    def confirm_set(self, event_id, expected_name, *, source, evidence):
        """Caller must supply an actual user confirmation or verified action.

        The observer never calls this based on its own transition inference.
        A future action executor must verify the selected card and placement.
        """
        if source not in ('user_confirmation', 'confirmed_action') or not evidence.strip():
            raise ValueError('Se requiere el origen y la evidencia de la confirmación')
        for card in self.cards.values():
            if card['event_id'] == event_id:
                if card['name'] != expected_name:
                    raise ValueError('La confirmación no coincide con la carta propuesta')
                card.update(source=source, confirmed=True, evidence=evidence)
                return self.snapshot()
        raise ValueError('La colocación ya no está en seguimiento')


def render_card_memory(memory):
    lines = []
    for card in memory.get('cards', []):
        zone = 'magias/trampas' if card['zone'] == 'MagicCards' else 'monstruos'
        confidence = 'confirmada' if card['confirmed'] else 'inferida, sin confirmar'
        lines.append(f'Memoria propia · {zone}, ref. {card["helper_index"] + 1}: '
                     f'{card["name"]} boca abajo ({confidence})')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='Reproducir seguimiento propio desde un JSONL, sin acceder al juego.')
    parser.add_argument('report', type=Path)
    args = parser.parse_args()
    tracker = OwnCardMemory()
    previous = None
    with args.report.open() as stream:
        for line in stream:
            state = json.loads(line)
            memory = tracker.observe(state, datetime.fromisoformat(state['timestamp']).timestamp())
            if memory != previous:
                print(state['timestamp'], render_card_memory(memory) or f'Seguimiento: {memory["status"]}; sin asociaciones')
            previous = memory


if __name__ == '__main__':
    main()
