"""晚场补传合并逻辑（纯函数，不触碰存储）。

原始成交与称重证据只增不改；可付款集、挂起票、终端水位均由原始证据派生，
因此重放、重试、乱序到达都得到同一结果。
"""
import json
from decimal import Decimal, ROUND_HALF_UP
from app.coerce import number, money
from app.errors import BusinessError

EVIDENCE_FIELDS = ('gross_kg', 'tare_kg')


def _norm_decimal(value):
    """数值归一：'115' 与 '115.0' 归一为同一文本，避免误报冲突。"""
    return format(Decimal(value).normalize(), 'f')


def canonical(tx):
    """把一条原始成交规范为可比较内容。

    返回 (canonical, None)；称重依据缺失或非法时返回 (None, 原因)。
    canonical 不含 ticket 之外的终端来源信息，保证跨终端同内容可去重。
    """
    if not isinstance(tx, dict):
        return None, '成交记录不是对象'
    ticket = str(tx.get('ticket', '')).strip()
    if not ticket:
        return None, '票号缺失'
    fish = str(tx.get('fish', '')).strip()
    if not fish:
        return None, '品类缺失'
    for field in EVIDENCE_FIELDS:
        raw = tx.get(field, None)
        if raw is None or str(raw).strip() == '':
            return None, f'缺少称重依据({field})'
    try:
        gross = number(tx.get('gross_kg'))
        tare = number(tx.get('tare_kg'))
        price = number(tx.get('price', ''))
        rate = number(tx.get('rate', '0'))
        paid = number(tx.get('paid', '0'))
    except BusinessError as error:
        return None, f'称重依据无效:{error.message}'
    if gross < 0 or tare < 0 or price < 0 or paid < 0:
        return None, '称重依据无效:数值为负'
    if not 0 <= rate < 1:
        return None, '称重依据无效:扣杂率越界'
    if gross < tare:
        return None, '称重依据无效:毛重小于皮重'
    return {
        'ticket': ticket, 'fish': fish,
        'gross_kg': _norm_decimal(gross), 'tare_kg': _norm_decimal(tare),
        'price': _norm_decimal(price), 'rate': _norm_decimal(rate),
        'paid': _norm_decimal(paid),
    }, None


def settle(canon):
    """与扣杂竞价同一公式：计价重=(毛重−皮重)×(1−扣杂率) 量化 0.001kg，金额四舍五入到分。"""
    net = Decimal(canon['gross_kg']) - Decimal(canon['tare_kg'])
    billable = (net * (1 - Decimal(canon['rate']))).quantize(Decimal('.001'), rounding=ROUND_HALF_UP)
    amount = money(billable * Decimal(canon['price']))
    due = money(Decimal(amount) - Decimal(canon['paid']))
    return {
        'ticket': canon['ticket'], 'fish': canon['fish'],
        'gross_kg': canon['gross_kg'], 'tare_kg': canon['tare_kg'],
        'net_kg': format(net.normalize(), 'f'), 'billable_kg': format(billable.normalize(), 'f'),
        'price': canon['price'], 'rate': canon['rate'],
        'amount': amount, 'paid': money(Decimal(canon['paid'])), 'due': due,
    }


def _content_key(canon):
    return json.dumps(canon, ensure_ascii=False, sort_keys=True)


def derive_ticket(ticket, rows):
    """汇总一票的全部原始记录，裁决为 merged（可付款）或 held（挂起）。

    rows: [{'terminal_id','seq','payload'(dict)}, ...]，按到达顺序。
    同内容重传去重；内容不一致或全部缺称重依据则挂起，任何记录都不丢弃。
    """
    good = {}
    broken = []
    for row in rows:
        canon, why = canonical(row['payload'])
        if canon is None:
            broken.append({'terminal_id': row['terminal_id'], 'seq': row['seq'], 'reason': why})
            continue
        key = _content_key(canon)
        good.setdefault(key, {'canonical': canon, 'sources': []})
        good[key]['sources'].append({'terminal_id': row['terminal_id'], 'seq': row['seq']})
    if len(good) == 1:
        entry = next(iter(good.values()))
        line = settle(entry['canonical'])
        line['terminals'] = sorted({s['terminal_id'] for s in entry['sources']})
        line['copies'] = len(entry['sources'])
        if broken:
            line['ignored_broken'] = len(broken)
        return {'state': 'merged', 'line': line}
    terminals = sorted({r['terminal_id'] for r in rows})
    if not good:
        detail = broken[0]['reason'] if broken else '缺少称重依据'
        return {'state': 'held', 'hold': {
            'ticket': ticket, 'code': 'MISSING_EVIDENCE', 'detail': detail,
            'terminals': terminals, 'records': len(rows)}}
    return {'state': 'held', 'hold': {
        'ticket': ticket, 'code': 'TICKET_CONFLICT',
        'detail': f'{len(good)} 种不一致内容，涉及 {len(terminals)} 台终端',
        'terminals': terminals, 'records': len(rows)}}


def derive_session(rows):
    """由全部原始成交派生可付款集与挂起票。rows 为 raw_transactions 行（payload 已解析）。"""
    by_ticket = {}
    for row in rows:
        by_ticket.setdefault(row['ticket'], []).append(row)
    payable, held = [], []
    for ticket in sorted(by_ticket):
        verdict = derive_ticket(ticket, by_ticket[ticket])
        if verdict['state'] == 'merged':
            payable.append(verdict['line'])
        else:
            held.append(verdict['hold'])
    total = sum((Decimal(l['amount']) for l in payable), Decimal(0))
    due = sum((Decimal(l['due']) for l in payable), Decimal(0))
    return {'payable': payable, 'held': held,
            'total': money(total), 'due_total': money(due)}


def watermark(seqs):
    """水位=从 1 起连续已应用的最大批次号；gaps=已见范围内的缺口批次。"""
    seen = set(seqs)
    level = 0
    while level + 1 in seen:
        level += 1
    top = max(seen) if seen else 0
    gaps = [n for n in range(1, top + 1) if n not in seen]
    return {'watermark': level, 'max_seen': top, 'gaps': gaps}


def compute_delta(old_lines, new_lines):
    """比较两个发布版本：新增票计入新增金额，消失票计入撤回金额，
    同票金额变化按撤回旧值+新增新值各计一次。"""
    old = {l['ticket']: l for l in old_lines}
    new = {l['ticket']: l for l in new_lines}
    added = sorted(t for t in new if t not in old)
    withdrawn = sorted(t for t in old if t not in new)
    changed = sorted(t for t in new if t in old and old[t]['amount'] != new[t]['amount'])
    added_amount = sum((Decimal(new[t]['amount']) for t in added + changed), Decimal(0))
    withdrawn_amount = sum((Decimal(old[t]['amount']) for t in withdrawn + changed), Decimal(0))
    return {
        'added': money(added_amount), 'withdrawn': money(withdrawn_amount),
        'added_tickets': sorted(added + changed), 'withdrawn_tickets': sorted(withdrawn + changed),
        'changed': changed,
    }
