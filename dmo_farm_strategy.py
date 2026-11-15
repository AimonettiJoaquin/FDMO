"""Numeric farm-deck tactics shared by summon, spell and battle decisions."""
from functools import lru_cache

from dmo_advisor import published_hand_actions, published_summons, summon_attack, visible, zone_cards

ASSAILANT = 'D.D. Assailant'
# Verified from global::Attributes defaults in the installed IL2CPP metadata.
EARTH = 0
EARTH_FARM_CARDS = {ASSAILANT, 'Gyaku-Gire Panda', 'Enraged Battle Ox',
                    'King Tiger Wanghu', 'Giant Rex', 'Sabersaurus', 'Chiron the Mage'}


def earth(card):
    attribute = card.get('attribute_raw')
    if type(attribute) is int:
        return attribute == EARTH
    return card.get('name') in EARTH_FARM_CARDS


def damage_route(attackers, defenders, *, ox=False, unattackable=()):
    """Best numeric damage using winning battles only; hidden stats stay unknown.

    At most five attackers/defenders. Panda loses 500 ATK as enemies leave.
    Returned moves also allow Battle to execute a lethal line in that order.
    """
    attackers, defenders = tuple(attackers), tuple(defenders)
    if len(attackers) > 5 or len(defenders) > 5:
        return 0, ()

    @lru_cache(None)
    def solve(available, remaining):
        best = (0, ())
        removed = len(defenders) - remaining.bit_count()
        for i, attacker in enumerate(attackers):
            if not available & (1 << i):
                continue
            atk = max(0, attacker['ATK'] -
                      (500 * removed if attacker.get('name') == 'Gyaku-Gire Panda' else 0))
            choices = []
            if not remaining:
                choices.append((None, atk, remaining))
            else:
                piercing = (attacker.get('name') == 'Gyaku-Gire Panda' or
                            ox and attacker.get('name') in
                            {'Enraged Battle Ox', 'Gyaku-Gire Panda', 'King Tiger Wanghu', 'Chiron the Mage'})
                for j, defender in enumerate(defenders):
                    if (not remaining & (1 << j) or not visible(defender) or
                            defender['address'] in unattackable):
                        continue
                    position = defender.get('face_position_raw')
                    stat = defender.get('ATK') if position == 1 else defender.get('DEF')
                    if position not in (1, 2) or type(stat) is not int or atk <= stat:
                        continue
                    damage = atk - stat if position == 1 or piercing else 0
                    choices.append((defender['address'], damage, remaining & ~(1 << j)))
            for target, damage, next_remaining in choices:
                future, route = solve(available & ~(1 << i), next_remaining)
                if damage + future > best[0]:
                    best = damage + future, ((attacker['address'], target),) + route
        return best

    return solve((1 << len(attackers)) - 1, (1 << len(defenders)) - 1)


class FarmCombat:
    def __init__(self, state):
        self.state = state
        self.own = next(f for f in state['board']['fields'] if f.get('local') is True)
        self.rival = next(f for f in state['board']['fields'] if f.get('local') is False)
        self.ours = zone_cards(self.own, 'MonsterCards')
        self.theirs = zone_cards(self.rival, 'MonsterCards')
        self.hand = self.own.get('HandCards') or []
        self._damage_cache = {}
        self.gaia_active = any(f.get('field_spell', {}).get('status') == 'observed' and
                               visible(f.get('field_spell', {}).get('card') or {}) and
                               f['field_spell']['card'].get('name') == 'Gaia Power'
                               for f in (self.own, self.rival))
        self.summons = []
        self.gaia_pending = False
        if state.get('phase') in ('MAIN 1', 'MAIN 2'):
            actions = state.get('actions')
            allowed = published_summons(actions, self.own, self.hand) if actions else {
                c['address'] for c in self.hand}
            self.summons = [c for c in self.hand if c['address'] in allowed and
                            type(c.get('Level')) is int and 1 <= c['Level'] <= 4 and
                            type(c.get('ATK')) is int] if len(self.ours) < 5 else []
            if actions:
                spells = published_hand_actions(actions, self.own, self.hand,
                    option_name='ActivateEffect', option_value=0)
                self.gaia_pending = (not self.gaia_active and
                    self.own.get('field_spell', {}).get('status') == 'observed' and
                    any(c.get('name') == 'Gaia Power' and c['address'] in spells for c in self.hand))

    def attack(self, card, *, hand=False, enemy_count=None):
        count = len(self.theirs) if enemy_count is None else enemy_count
        attack = summon_attack(card, count) if hand else card['ATK']
        if not hand and card.get('name') == 'Gyaku-Gire Panda':
            attack += 500 * (count - len(self.theirs))
        if earth(card) and (self.gaia_pending or hand and self.gaia_active):
            attack += 500
        return max(0, attack)

    def defender(self, card):
        projected = dict(card)
        if self.gaia_pending and visible(card) and earth(card):
            if type(card.get('ATK')) is int:
                projected['ATK'] = card['ATK'] + 500
            if type(card.get('DEF')) is int:
                projected['DEF'] = max(0, card['DEF'] - 400)
        return projected

    def strongest(self):
        attacks = [self.attack(c) for c in self.ours if visible(c) and type(c.get('ATK')) is int]
        attacks += [self.attack(c, hand=True) for c in self.summons]
        return max(attacks, default=0)

    def threats(self):
        strongest = self.strongest()
        result = []
        for card in self.theirs:
            if not visible(card):
                continue
            defender = self.defender(card)
            atk = defender.get('ATK')
            stat = atk if defender.get('face_position_raw') == 1 else defender.get('DEF')
            if (type(atk) is int and atk > strongest and type(stat) is int and
                    defender.get('face_position_raw') in (1, 2) and stat >= strongest):
                result.append(card)
        return result

    def trade(self, attackers=None, *, excluded_targets=()):
        if self.state.get('phase') not in ('MAIN 1', 'BATTLE'):
            return None
        candidates = (self.ours + self.summons) if attackers is None else attackers
        candidates = [c for c in candidates if c.get('name') == ASSAILANT and
                      (c in self.summons or c.get('face_position_raw') == 1 and
                       c.get('CanAttack') is True and c.get('AttackedThisTurn') is False)]
        pairs = []
        for target in self.threats():
            if target.get('face_position_raw') != 1 or target['address'] in excluded_targets:
                continue  # Hitting a higher DEF does not destroy Assailant.
            for card in candidates:
                loss = self.defender(target)['ATK'] - self.attack(card, hand=card in self.summons)
                lp = self.own.get('life_points_candidate')
                if type(lp) is int and 0 <= loss < lp:
                    pairs.append((self.defender(target)['ATK'], -loss, card, target, loss))
        if not pairs:
            return None
        _, _, card, target, loss = max(pairs, key=lambda p: p[:2])
        return dict(attacker=card, target=target, self_damage=loss)

    def needs_assailant_search(self):
        if (len(self.ours) >= 5 or self.state.get('phase') != 'MAIN 1' or
                any(c.get('name') == ASSAILANT for c in self.ours + self.hand)):
            return False
        lp = self.own.get('life_points_candidate')
        attack = 1700 + (500 if self.gaia_active or self.gaia_pending else 0)
        return type(lp) is int and any(t.get('face_position_raw') == 1 and
            0 <= self.defender(t)['ATK'] - attack < lp for t in self.threats())

    def damage(self, removed=()):
        if self.state.get('phase') != 'MAIN 1':
            return 0
        removed = frozenset(removed)
        if removed in self._damage_cache:
            return self._damage_cache[removed]
        defenders = [self.defender(c) for c in self.theirs if c['address'] not in removed]
        ours = [c for c in self.ours if visible(c) and c.get('face_position_raw') == 1 and
                c.get('CanAttack') is True and c.get('AttackedThisTurn') is False and
                type(c.get('ATK')) is int]
        groups = [ours] + [ours + [c] for c in self.summons]
        best = 0
        for group in groups:
            attackers = [dict(c, ATK=self.attack(c, hand=c in self.summons,
                                               enemy_count=len(defenders))) for c in group]
            ox = any(c.get('name') == 'Enraged Battle Ox' and visible(c) for c in group)
            best = max(best, damage_route(attackers, defenders, ox=ox)[0])
        self._damage_cache[removed] = best
        return best

    def removal_reason(self, name):
        if not self.theirs:
            return None
        if name == 'Raigeki':
            outcomes = [{c['address'] for c in self.theirs}]
        else:
            faceup = [c for c in self.theirs if visible(c)]
            if not faceup or any(type(c.get('DEF')) is not int for c in faceup):
                return None
            highest = max(self.defender(c)['DEF'] for c in faceup)
            outcomes = [{c['address']} for c in faceup if self.defender(c)['DEF'] == highest]
        lp = self.rival.get('life_points_candidate')
        if (type(lp) is int and lp > 0 and self.damage() < lp and
                all(self.damage(outcome) >= lp for outcome in outcomes)):
            return 'lethal'
        threats = {c['address'] for c in self.threats()}
        if threats and self.trade() is None and all(outcome & threats for outcome in outcomes):
            return 'unbeatable'
        return None
