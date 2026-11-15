"""Session match results from verified duel exit and observed final life points."""


class MatchStats:
    def __init__(self):
        self.started = self.wins = self.losses = self.forfeits = self.unknown = 0
        self.current = None

    def start(self):
        if self.current is not None:
            raise RuntimeError('Hay una partida sin cerrar en las estadísticas')
        self.started += 1
        self.current = dict(manager=None, seen=False, outside=False, last=None,
                            stable=0, result=None, forfeited=False)

    def observe(self, state, result=None):
        match = self.current
        if match is None:
            return
        if result == 'fallback_forfeit_confirm_clicked':
            match['forfeited'] = True
        if state.get('status') == 'outside_duel':
            if match['seen']:
                match['outside'] = True
            return
        if state.get('status') != 'duel' or state.get('local_player') is not True:
            return
        manager = state.get('manager')
        if manager in (None, '0x0'):
            return
        if match['manager'] is None:
            match['manager'] = manager
        if match['manager'] != manager:
            return
        match['seen'] = True
        fields = state.get('board', {}).get('fields', [])
        own = [f for f in fields if f.get('local') is True]
        rival = [f for f in fields if f.get('local') is False]
        if len(own) != 1 or len(rival) != 1:
            return
        own_lp, rival_lp = own[0].get('life_points_candidate'), rival[0].get('life_points_candidate')
        if (type(own_lp) is not int or type(rival_lp) is not int or
                not 0 <= own_lp <= 99999 or not 0 <= rival_lp <= 99999 or
                not own[0].get('address') or not rival[0].get('address')):
            return
        candidate = (own[0]['address'], rival[0]['address'], own_lp, rival_lp)
        if candidate == match['last']:
            match['stable'] += 1
        else:
            match['last'], match['stable'] = candidate, 1
        if match['stable'] >= 2:
            if own_lp > 0 and rival_lp == 0:
                match['result'] = 'win'
            elif own_lp == 0 and rival_lp > 0:
                match['result'] = 'loss'

    def finish(self):
        match = self.current
        if match is None:
            return
        if match['seen'] and match['outside'] and match['forfeited']:
            self.forfeits += 1
        elif match['seen'] and match['outside'] and match['result'] == 'win':
            self.wins += 1
        elif match['seen'] and match['outside'] and match['result'] == 'loss':
            self.losses += 1
        else:
            self.unknown += 1
        self.current = None

    def summary(self):
        return (f'Partidas: {self.started} · Ganadas: {self.wins} · Perdidas: {self.losses} '
                f'· Abandonadas: {self.forfeits} · Sin resultado: {self.unknown}')
