"""晚场补传存储层：批次幂等接入、原子发布、证据保全。

表结构（与结算流水线同一 SQLite 文件）：
- sync_sessions     场次登记
- uploads           补传批次，(场次,终端,批次号) 唯一，原始报文留存
- raw_transactions  原始成交与称重证据，只增不改
- publications      可付款结果发布版本，只增不改；当前版=最大版本号
"""
import hashlib
import json
from datetime import datetime, timezone
from app import store
from app.errors import BusinessError
from app.session_logic import derive_session, watermark, compute_delta

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_sessions(
  session_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads(
  session_id TEXT NOT NULL,
  terminal_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  payload TEXT NOT NULL,
  batch_hash TEXT NOT NULL,
  tx_count INTEGER NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY(session_id, terminal_id, seq)
);
CREATE TABLE IF NOT EXISTS raw_transactions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  terminal_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  ticket TEXT NOT NULL,
  payload TEXT NOT NULL,
  received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_ticket ON raw_transactions(session_id, ticket);
CREATE TABLE IF NOT EXISTS publications(
  session_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  lines TEXT NOT NULL,
  total TEXT NOT NULL,
  added TEXT NOT NULL,
  withdrawn TEXT NOT NULL,
  delta TEXT NOT NULL,
  seal TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(session_id, version)
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def connect(path):
    con = store.connect(path)
    con.isolation_level = None  # 显式事务：BEGIN IMMEDIATE ... COMMIT/ROLLBACK
    con.executescript(SCHEMA)
    return con


class _Tx:
    """显式事务上下文：正常退出 COMMIT，异常 ROLLBACK，保证失败不留半提交。"""

    def __init__(self, con):
        self.con = con
        self.active = False

    def __enter__(self):
        self.con.execute('BEGIN IMMEDIATE')
        self.active = True
        return self

    def __getattr__(self, name):
        return getattr(self.con, name)

    def commit(self):
        self.con.execute('COMMIT')
        self.active = False

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.active:
                if exc_type is None:
                    self.con.execute('COMMIT')
                else:
                    self.con.execute('ROLLBACK')
        finally:
            self.active = False
            self.con.close()
        return False


def _batch_hash(terminal_id, seq, transactions):
    canonical = json.dumps(
        sorted((json.dumps(t, ensure_ascii=False, sort_keys=True) for t in transactions)),
        ensure_ascii=False)
    return hashlib.sha256(f'{terminal_id}|{seq}|{canonical}'.encode()).hexdigest()


def _validate_upload(session_id, terminal_id, seq, transactions):
    if not session_id:
        raise BusinessError('BAD_UPLOAD', '场次编号不能为空')
    if not terminal_id:
        raise BusinessError('BAD_UPLOAD', '终端编号不能为空')
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise BusinessError('BAD_UPLOAD', '批次号须为正整数')
    if not isinstance(transactions, list) or not transactions:
        raise BusinessError('BAD_UPLOAD', '补传批次须至少含一笔成交')
    for tx in transactions:
        if not isinstance(tx, dict) or not str(tx.get('ticket', '')).strip():
            raise BusinessError('BAD_UPLOAD', '成交记录缺少票号')


def ingest(path, session_id, terminal_id, seq, transactions, sent_at=None):
    """接入一批补传成交。同终端同批次号重试：内容一致原样确认（不新增成交），
    内容不一致拒绝（409），两种情况下已存证据都不动。"""
    session_id = str(session_id or '').strip()
    terminal_id = str(terminal_id or '').strip()
    _validate_upload(session_id, terminal_id, seq, transactions)
    digest = _batch_hash(terminal_id, seq, transactions)
    received = _now()
    with _Tx(connect(path)) as con:
        con.execute('INSERT OR IGNORE INTO sync_sessions(session_id,created_at) VALUES(?,?)',
                    (session_id, received))
        row = con.execute(
            'SELECT batch_hash,received_at FROM uploads WHERE session_id=? AND terminal_id=? AND seq=?',
            (session_id, terminal_id, seq)).fetchone()
        if row:
            if row['batch_hash'] != digest:
                raise BusinessError('SEQ_CONFLICT',
                                    f'终端 {terminal_id} 批次 {seq} 已存在且内容不同，已拒绝', 409)
            return {'status': 'duplicate', 'session_id': session_id, 'terminal_id': terminal_id,
                    'seq': seq, 'received': 0, 'first_received_at': row['received_at']}
        payload = json.dumps({'terminal_id': terminal_id, 'seq': seq, 'sent_at': sent_at,
                              'transactions': transactions}, ensure_ascii=False)
        con.execute(
            'INSERT INTO uploads(session_id,terminal_id,seq,payload,batch_hash,tx_count,received_at)'
            ' VALUES(?,?,?,?,?,?,?)',
            (session_id, terminal_id, seq, payload, digest, len(transactions), received))
        for tx in transactions:
            con.execute(
                'INSERT INTO raw_transactions(session_id,terminal_id,seq,ticket,payload,received_at)'
                ' VALUES(?,?,?,?,?,?)',
                (session_id, terminal_id, seq, str(tx['ticket']).strip(),
                 json.dumps(tx, ensure_ascii=False), received))
    return {'status': 'applied', 'session_id': session_id, 'terminal_id': terminal_id,
            'seq': seq, 'received': len(transactions)}


def _load_derived(con, session_id):
    rows = [{'terminal_id': r['terminal_id'], 'seq': r['seq'], 'ticket': r['ticket'],
             'payload': json.loads(r['payload'])}
            for r in con.execute(
                'SELECT terminal_id,seq,ticket,payload FROM raw_transactions WHERE session_id=? ORDER BY id',
                (session_id,))]
    return derive_session(rows)


def _seal(lines):
    return hashlib.sha256(json.dumps(lines, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def publish(path, session_id, expect_version=None):
    """发布当前可付款结果为新版本。整个发布在单事务内完成：
    任一步失败回滚，已发布旧版保持不变；内容未变时不产生新版本。"""
    con = connect(path)
    with _Tx(con) as tx:
        count = tx.execute('SELECT COUNT(*) AS n FROM raw_transactions WHERE session_id=?',
                           (session_id,)).fetchone()['n']
        if not count:
            raise BusinessError('NOTHING_TO_PUBLISH', '场次暂无可发布的成交')
        derived = _load_derived(tx, session_id)
        lines = derived['payable']
        seal = _seal(lines)
        current = tx.execute(
            'SELECT * FROM publications WHERE session_id=? ORDER BY version DESC LIMIT 1',
            (session_id,)).fetchone()
        current_version = current['version'] if current else 0
        if expect_version is not None and expect_version != current_version:
            raise BusinessError('STALE_VERSION',
                                f'场次发布版本已更新（当前 v{current_version}），请刷新', 409)
        if current and current['seal'] == seal:
            tx.commit()
            return _publication_dict(current) | {'unchanged': True}
        delta = compute_delta(json.loads(current['lines']) if current else [], lines)
        tx.execute(
            'INSERT INTO publications(session_id,version,lines,total,added,withdrawn,delta,seal,created_at)'
            ' VALUES(?,?,?,?,?,?,?,?,?)',
            (session_id, current_version + 1, json.dumps(lines, ensure_ascii=False),
             derived['total'], delta['added'], delta['withdrawn'],
             json.dumps(delta, ensure_ascii=False), seal, _now()))
    return get_publication(path, session_id, current_version + 1)


def _publication_dict(row):
    return {'session_id': row['session_id'], 'version': row['version'],
            'lines': json.loads(row['lines']), 'total': row['total'],
            'added': row['added'], 'withdrawn': row['withdrawn'],
            'delta': json.loads(row['delta']), 'seal': row['seal'],
            'created_at': row['created_at'], 'unchanged': False}


def get_publication(path, session_id, version=None):
    con = connect(path)
    try:
        if version is None:
            row = con.execute(
                'SELECT * FROM publications WHERE session_id=? ORDER BY version DESC LIMIT 1',
                (session_id,)).fetchone()
        else:
            row = con.execute(
                'SELECT * FROM publications WHERE session_id=? AND version=?',
                (session_id, version)).fetchone()
        if not row:
            raise BusinessError('NOT_FOUND', '发布版本不存在', 404)
        return _publication_dict(row)
    finally:
        con.close()


def sessions(path):
    con = connect(path)
    try:
        return [dict(r) for r in con.execute(
            'SELECT session_id,created_at FROM sync_sessions ORDER BY rowid DESC')]
    finally:
        con.close()


def evidence(path, session_id, ticket=None):
    """原始成交与称重证据清单（含被挂起与重复记录），证明无任何丢失。"""
    con = connect(path)
    try:
        sql = ('SELECT id,terminal_id,seq,ticket,payload,received_at FROM raw_transactions'
               ' WHERE session_id=?' + (' AND ticket=?' if ticket else '') + ' ORDER BY id')
        args = (session_id, ticket) if ticket else (session_id,)
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def state(path, session_id):
    """核对页所需的完整场次状态：水位、待补票、可付款集、发布版本与本次变动。"""
    con = connect(path)
    try:
        exists = con.execute('SELECT 1 FROM sync_sessions WHERE session_id=?',
                             (session_id,)).fetchone()
        if not exists:
            return {'exists': False, 'session_id': session_id}
        uploads = [dict(r) for r in con.execute(
            'SELECT terminal_id,seq,tx_count,received_at FROM uploads WHERE session_id=?'
            ' ORDER BY terminal_id,seq', (session_id,))]
        raw_count = con.execute('SELECT COUNT(*) AS n FROM raw_transactions WHERE session_id=?',
                                (session_id,)).fetchone()['n']
        derived = _load_derived(con, session_id)
        pubs = [dict(r) for r in con.execute(
            'SELECT version,lines,total,added,withdrawn,delta,seal,created_at FROM publications'
            ' WHERE session_id=? ORDER BY version DESC', (session_id,))]
    finally:
        con.close()
    by_terminal = {}
    for up in uploads:
        slot = by_terminal.setdefault(up['terminal_id'],
                                      {'seqs': [], 'batches': 0, 'tickets': 0, 'last_received_at': ''})
        slot['seqs'].append(up['seq'])
        slot['batches'] += 1
        slot['tickets'] += up['tx_count']
        slot['last_received_at'] = max(slot['last_received_at'], up['received_at'])
    terminals = []
    for terminal_id in sorted(by_terminal):
        slot = by_terminal[terminal_id]
        levels = watermark(slot['seqs'])
        terminals.append({'terminal_id': terminal_id, **levels,
                          'batches': slot['batches'], 'tickets': slot['tickets'],
                          'last_received_at': slot['last_received_at']})
    for pub in pubs:
        pub['delta'] = json.loads(pub['delta'])
    current = None
    if pubs:
        current = pubs[0]
        current['lines'] = json.loads(current['lines'])
        for pub in pubs[1:]:
            del pub['lines']
    unpublished = (not pubs and raw_count > 0) or \
                  (bool(pubs) and _seal(derived['payable']) != pubs[0]['seal'])
    return {
        'exists': True, 'session_id': session_id,
        'terminals': terminals,
        'payable': derived['payable'], 'total': derived['total'], 'due_total': derived['due_total'],
        'held': derived['held'], 'held_count': len(derived['held']),
        'raw_count': raw_count,
        'current': current, 'publications': pubs,
        'unpublished_changes': unpublished,
    }
