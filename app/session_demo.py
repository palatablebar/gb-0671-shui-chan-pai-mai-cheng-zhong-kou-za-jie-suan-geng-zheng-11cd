"""晚场演示场次：断网→补传→冲突→补证→发布的完整剧本。

全部通过真实 ingest/publish 路径执行；场次已存在时直接返回现状，
重复调用不会重演（补传幂等 + 发布内容未变不增版）。
"""
from app import session_store

DEMO_SESSION = 'evening-demo'

_FISH = '黄鱼'
_PRICE = '12.30'
_RATE = '0.05'


def _tx(ticket, gross, tare, paid='0'):
    tx = {'ticket': ticket, 'fish': _FISH, 'price': _PRICE, 'rate': _RATE, 'paid': paid}
    if gross is not None:
        tx['gross_kg'] = gross
    if tare is not None:
        tx['tare_kg'] = tare
    return tx


def script():
    """(动作, 参数) 序列：先同步 4 票发布 v1，再冲突/补证/跳号，最后发布 v2。"""
    return [
        ('upload', ('手持01', 1, [_tx('00101', '115', '15'), _tx('00102', '110', '10')])),
        ('upload', ('手持02', 1, [_tx('00201', '108', '8'), _tx('00202', '120', None)])),
        ('upload', ('手持03', 1, [_tx('00301', '105', '5')])),
        ('publish', None),
        ('upload', ('手持01', 1, [_tx('00101', '115', '15'), _tx('00102', '110', '10')])),  # 断网重试
        ('upload', ('手持02', 2, [_tx('00101', '118', '15')])),  # 跨终端票号冲突
        ('upload', ('手持03', 2, [_tx('00202', '120', '20')])),  # 补称重依据
        ('upload', ('手持01', 3, [_tx('00103', '100', '0')])),  # 手持01 批次2 未到，形成缺口
        ('publish', None),
    ]


def seed_demo(path):
    if any(s['session_id'] == DEMO_SESSION for s in session_store.sessions(path)):
        return session_store.state(path, DEMO_SESSION)
    for action, params in script():
        if action == 'upload':
            terminal, seq, txs = params
            session_store.ingest(path, DEMO_SESSION, terminal, seq, txs)
        else:
            session_store.publish(path, DEMO_SESSION)
    return session_store.state(path, DEMO_SESSION)
