"""Handle the client's card selection panel independently of turns/effects."""
import re

from dmo_memory_bot import require_recent


class CardPanelController:
    def __init__(self, desktop, config, *, preferred_name=None, discard=False):
        self.desktop = desktop
        self.width, self.height = config['screen']['width'], config['screen']['height']
        self.preferred_name = preferred_name
        self.discard = discard
        self.identity = None
        self.cards = None
        self.desired = None
        self.stable = 0
        self.last_selection = None
        self.selection_stable = 0
        self.pending_card = None
        self.confirmed = False
        self.finished = False
        self.waited = 0
        self.scrolled_right = False
        self.scroll_wait = 0

    def click(self, state, point):
        require_recent(state)
        point = (round(point[0] * self.width / 1920), round(point[1] * self.height / 1080))
        if not 0 <= point[0] < self.width or not 0 <= point[1] < self.height:
            raise RuntimeError('Punto del panel fuera de pantalla')
        actual = self.desktop.move(point)
        if self.desktop.position() != actual:
            raise RuntimeError('El cursor cambió antes del clic en el panel')
        self.desktop.click()

    def parse(self, state):
        actions = state.get('actions', {})
        flags, prompts = actions.get('flags', {}), actions.get('prompts', {})
        pointer = prompts.get('CurrentOptionHelper')
        if (state.get('status') != 'duel' or state.get('local_player') is not True or
                actions.get('status') != 'observed' or pointer in (None, '0x0') or
                prompts.get('CurrentChoiceHelper') != '0x0' or
                prompts.get('CurrentConfirmationHelper') != '0x0' or
                flags.get('GameStateOpen') is not False or
                any(flags.get(k) is not False for k in ('Busy', 'SelectingCard')) or
                type(flags.get('ShowChainPrompt')) is not bool):
            return None
        # The captured Terraforming panel remains usable while the optional
        # Chain? (X) control is visible. Validate the actual option panel below
        # rather than treating this flag alone as a blocking modal prompt.
        details = actions.get('prompt_details', {}).get('CurrentOptionHelper', {})
        cards, selected = details.get('cards'), details.get('selected_indices')
        minimum, maximum = details.get('MinimumRequiredOptions'), details.get('MaximumRequiredOptions')
        if (not isinstance(cards, list) or not cards or not isinstance(selected, list) or
                type(minimum) is not int or type(maximum) is not int or
                not 0 <= minimum <= maximum <= len(cards) or maximum < 1 or
                any(c.get('ordinal') != i or not c.get('name') or c.get('address') in (None, '0x0') or
                    type(c.get('Index')) is not int or c['Index'] < 0 for i, c in enumerate(cards)) or
                len({c['address'] for c in cards}) != len(cards) or
                len({c['Index'] for c in cards}) != len(cards) or
                any(type(i) is not int for i in selected) or len(set(selected)) != len(selected)):
            return None
        text = ' '.join(re.sub(r'<[^>]+>', '', str(details.get(k) or '')) for k in ('Title', 'Description'))
        # Nine-warrior ROTA panel failures preceded six forfeits in the last session.
        # Use the first seven cards of the existing left-aligned layout;
        # their selected index must still be observed before confirmation.
        nine_warriors = (len(cards) == 9 and not self.discard and
                         minimum == maximum == 1 and 'Reinforcement of the Army' in text)
        if len(cards) > 8 and not nine_warriors:
            raise RuntimeError('Panel de cartas con más de 8 opciones: falta validar el desplazamiento del panel')
        if self.discard:
            if minimum != 2 or maximum != 2:
                return None
            locations = [c.get('CardLocation') for c in cards]
            if all(location == dict(enum_raw=0) for location in locations):
                # Graceful publishes UI GameCard copies with location Any.
                # Validate their order and names against the complete live
                # hand; UI addresses are not the real hand card addresses.
                own = [f for f in state.get('board', {}).get('fields', []) if f.get('local') is True]
                if len(own) != 1:
                    return None
                hand = own[0].get('HandCards')
                if (not isinstance(hand, list) or len(hand) != own[0].get('HandCardCount') or
                        len(hand) != len(cards) or
                        any(not c or c.get('unknown') or c.get('location') != 'Hand' for c in hand) or
                        [c.get('name') for c in hand] != [c['name'] for c in cards]):
                    return None
            elif any(location != dict(enum_raw=1) for location in locations):
                return None
        return pointer, cards, selected, minimum, maximum, text

    def rank(self, cards, text):
        preferred = self.preferred_name
        if 'Terraforming' in text:
            preferred = 'Gaia Power'
        elif 'Tenki' in text:
            preferred = 'Enraged Battle Ox'
        # Only rank client-offered cards; never invent eligibility. Search and
        # revival prefer strength, while an explicit discard prefers low ATK.
        discard = self.discard or 'discard' in text.lower()
        def score(card):
            atk = card.get('ATK') if type(card.get('ATK')) is int else 0
            defense = card.get('DEF') if type(card.get('DEF')) is int else 0
            if discard:
                # Preserve searches and draw spells rather than discarding
                # every spell just because its printed ATK is zero.
                keep = {'Reinforcement of the Army': 10000, 'Fire Formation - Tenki': 10000,
                        'Terraforming': 10000, 'Pot of Greed': 9000, 'Upstart Goblin': 9000,
                        'Graceful Charity': 8000, 'Gaia Power': 6500, 'Raigeki': 6000,
                        'Smashing Ground': 5000, 'Mystical Space Typhoon': 5000,
                        'Nobleman of Crossout': 5000, 'D.D. Assailant': 5500,
                        'Big Bang Shot': 4000}.get(card['name'], 1000 + atk)
                return (-keep, -defense,
                        -abs(card['ordinal'] - (len(cards) - 1) / 2), -card['ordinal'])
            return (card['name'] == preferred, -atk if discard else atk,
                    -defense if discard else defense,
                    -abs(card['ordinal'] - (len(cards) - 1) / 2), -card['ordinal'])
        return sorted(cards, key=score, reverse=True)

    def decode_selection(self, cards, selected):
        result = set()
        for value in selected:
            matches = [c for c in cards if c['Index'] == value or c['ordinal'] == value]
            if not matches or len({c['name'] for c in matches}) != 1:
                raise RuntimeError('Índice seleccionado ambiguo en el panel de cartas')
            match = next((c for c in matches if c['ordinal'] in self.desired), matches[0])
            result.add(match['ordinal'])
        if len(result) != len(selected):
            raise RuntimeError('Selecciones duplicadas en el panel de cartas')
        return result

    def observe(self, state):
        if self.finished:
            return None
        self.waited += 1
        pointer = state.get('actions', {}).get('prompts', {}).get('CurrentOptionHelper')
        if self.identity is not None and pointer != self.identity[2]:
            if not self.confirmed:
                raise RuntimeError('El panel de cartas cambió antes de confirmar la selección')
            self.finished = True
            return 'panel_verified'
        if self.waited >= 40:
            raise RuntimeError('No se verificó la selección o cierre del panel de cartas; no repetir clics')
        parsed = self.parse(state)
        if parsed is None:
            self.stable = self.selection_stable = 0
            return 'waiting_card_panel'
        pointer, cards, selected, minimum, maximum, text = parsed
        identity = (state['player'], state['manager'], pointer, minimum, maximum,
                    tuple((c['address'], c['Index'], c['name'], c.get('ATK'), c.get('DEF')) for c in cards), text)
        if self.identity is not None and identity != self.identity:
            raise RuntimeError('Cambió el contenido del panel durante la selección')
        if self.identity is None:
            self.identity, self.cards = identity, cards
            count = max(1, minimum)
            eligible = cards[:7] if len(cards) == 9 else cards
            self.desired = {c['ordinal'] for c in self.rank(eligible, text)[:count]}
        self.stable += 1
        chosen = self.decode_selection(cards, selected)
        if chosen - self.desired:
            raise RuntimeError('El panel tiene seleccionada una carta distinta del plan')
        if self.scroll_wait:
            self.scroll_wait -= 1
            return 'waiting_panel_scroll'
        if self.confirmed:
            return 'waiting_panel_result'
        if self.pending_card is not None:
            if self.pending_card not in chosen:
                return 'waiting_panel_selection'
            self.pending_card = None
            self.waited = 0
        if chosen != self.desired:
            self.last_selection, self.selection_stable = None, 0
            if self.stable < 2:
                return 'checking_card_panel'
            ordinal = min(self.desired - chosen)
            if len(cards) >= 8:
                # Graceful capture: first center (459,865), stride 163;
                # the eighth card is clipped beyond the viewport at x=1600.
                if ordinal == 7 and not self.scrolled_right:
                    # Click the visible right-hand scrollbar track, clear of
                    # every card. Wait for scrolling before choosing the card.
                    self.click(state, (1495, 992))
                    self.scrolled_right, self.scroll_wait, self.waited = True, 2, 0
                    return 'panel_scrolled_right'
                # At the right edge the last card fits inside the viewport
                # at x=1464. A page scroll can stop slightly short of the edge;
                # this point still lies well inside that card's visible area.
                first = 323 if self.scrolled_right else 459
                x, y = first + ordinal * 163, 865
            else:
                x, y = 960 + (ordinal - (len(cards) - 1) / 2) * 163, 890
            if not 380 < x < 1540:
                raise RuntimeError('La opción del panel requiere desplazamiento antes de seleccionarla')
            self.click(state, (x, y))
            self.pending_card, self.waited = ordinal, 0
            return 'panel_card_clicked: ' + cards[ordinal]['name']
        self.selection_stable = self.selection_stable + 1 if chosen == self.last_selection else 1
        self.last_selection = chosen
        if self.selection_stable < 2:
            return 'checking_panel_selection'
        self.click(state, (960, 1038))
        self.confirmed, self.waited = True, 0
        return 'panel_select_clicked'
