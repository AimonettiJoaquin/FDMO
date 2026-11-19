"""Elapsed session time and net observed currency per minute."""
import time


class SessionMetrics:
    def __init__(self, *, clock=None):
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.identity = None
        self.initial_money = self.final_money = None
        self.observations = 0
        self.first_observed = self.last_observed = None
        self.invalid_reason = None
        self.latest_balance_available = False
        self.last_sample_unstable = False
        self.last_read_error = None

    def observe(self, state):
        previous_available = self.latest_balance_available
        self.latest_balance_available = False
        self.last_sample_unstable = False
        process = state.get('process_identity', {})
        if not isinstance(process, dict):
            return
        keys = ('pid', 'process_start', 'binary_sha256')
        if any(process.get(k) is None for k in keys):
            return
        identity = tuple(process[k] for k in keys) + (state.get('character'),)
        if self.identity is not None and identity != self.identity:
            self.invalid_reason = 'Cambió el proceso o personaje durante la medición'
            return
        if (state.get('status') == 'unstable' and 'currency' not in state and
                self.identity == identity and self.invalid_reason is None):
            # A torn duel snapshot does not attempt a wallet read. Preserve
            # the preceding successful balance briefly, with an explicit age,
            # rather than losing the entire session metric on Ctrl+C.
            self.latest_balance_available = previous_available
            self.last_sample_unstable = True
            return
        wallet = state.get('currency', {})
        if not isinstance(wallet, dict):
            return
        self.last_read_error = wallet.get('error') if wallet.get('status') != 'observed' else None
        money = wallet.get('money')
        if (self.invalid_reason or state.get('local_player') is not True or
                state.get('status') not in ('duel', 'outside_duel', 'starting', 'transition') or
                wallet.get('status') != 'observed' or wallet.get('player') != state.get('player') or
                type(money) is not int or not 0 <= money <= 2**31 - 1):
            return
        now = self.clock()
        if self.initial_money is None:
            self.identity = identity
            self.initial_money = money
            self.first_observed = now
        self.final_money = money
        self.last_observed = now
        self.observations += 1
        self.latest_balance_available = True

    def report(self):
        now = self.clock()
        duration = max(0, now - self.started)
        hours, remainder = divmod(int(duration), 3600)
        minutes, seconds = divmod(remainder, 60)
        balance_age = max(0, now - self.last_observed) if self.last_observed is not None else None
        valid = (self.observations >= 2 and self.latest_balance_available and
                 self.invalid_reason is None and
                 (not self.last_sample_unstable or balance_age is not None and balance_age <= 3))
        recent = valid and self.last_sample_unstable
        generated = self.final_money - self.initial_money if valid else None
        return dict(duration_seconds=round(duration, 2),
                    duration_hms=f'{hours:02d}:{minutes:02d}:{seconds:02d}',
                    initial_money=self.initial_money, final_money=self.final_money,
                    money_generated=generated,
                    money_per_minute=round(generated * 60 / duration, 2)
                    if valid and duration > 0 else None,
                    money_metric_status=('invalid' if self.invalid_reason else
                                         'observed_recent' if recent else
                                         'observed' if valid else 'insufficient_data'),
                    money_metric_uses_recent_balance=recent,
                    money_metric_error=self.invalid_reason or self.last_read_error,
                    money_observations=self.observations,
                    money_first_observation_seconds=round(self.first_observed - self.started, 2)
                    if self.first_observed is not None else None,
                    money_last_observation_age_seconds=round(balance_age, 2)
                    if balance_age is not None else None,
                    money_metric_basis='last_observed_balance_minus_first_over_total_session_time')

    def summary(self, report=None):
        report = self.report() if report is None else report
        money, rate = report['money_generated'], report['money_per_minute']
        earned = f'{money:+d}' if money is not None else 'no disponible'
        per_minute = f'{rate:.2f}' if rate is not None else 'no disponible'
        summary = (f"Tiempo: {report['duration_hms']} · Dinero neto: {earned} "
                   f'· Dinero/min: {per_minute}')
        if report.get('money_metric_uses_recent_balance'):
            summary += f" · saldo de hace {report['money_last_observation_age_seconds']:.1f} s"
        return summary
