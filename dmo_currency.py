"""Validated, read-only wallet sampling shared by the farming monitor."""


def read_wallet(monitor):
    player = monitor.player
    if not player or not monitor.matches_player(player):
        raise ValueError('No hay un jugador local validado para leer el saldo')
    misc = monitor.pointer(player, 'DuelistScript', 'misc')
    monitor.check_object(misc, 'global::Misc')
    if monitor.pointer(misc, 'global::Misc', 'duelistScript') != player:
        raise ValueError('El saldo no está vinculado al jugador local')
    money = monitor.field(misc, 'global::Misc', 'Money')
    if type(money) is not int or not 0 <= money <= 2**31 - 1:
        raise ValueError('Saldo inválido')
    if (monitor.field(misc, 'global::Misc', 'Money') != money or
            monitor.pointer(player, 'DuelistScript', 'misc') != misc or
            monitor.pointer(misc, 'global::Misc', 'duelistScript') != player or
            not monitor.matches_player(player)):
        raise ValueError('El saldo o su propietario cambió durante la lectura')
    return dict(status='observed', money=money, player=hex(player), misc=hex(misc),
                source='Misc.Money', persistence_verified=False)
