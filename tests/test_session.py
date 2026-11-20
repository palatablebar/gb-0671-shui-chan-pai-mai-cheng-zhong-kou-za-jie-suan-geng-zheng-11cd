"""晚场补传：幂等补传、冲突挂起、缺证挂起、原子发布、证据保全。"""
import pytest
from fastapi.testclient import TestClient
from app import main, session_store, session_logic
from app.session_logic import canonical, derive_ticket, watermark, compute_delta, settle
from app.errors import BusinessError

TX = {'ticket': '00101', 'fish': '黄鱼', 'gross_kg': '115', 'tare_kg': '15',
      'price': '12.30', 'rate': '0.05', 'paid': '0'}


def tx(ticket, gross='115', tare='15', **extra):
    base = dict(TX, ticket=ticket)
    if gross is not None:
        base['gross_kg'] = gross
    else:
        base.pop('gross_kg', None)
    if tare is not None:
        base['tare_kg'] = tare
    else:
        base.pop('tare_kg', None)
    base.update(extra)
    return base


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / 'session.sqlite')


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'web.sqlite'))
    return TestClient(main.app)


# ---------- 纯逻辑 ----------

def test_canonical_normalizes_equivalent_numbers():
    a, _ = canonical(tx('00101'))
    b, _ = canonical(tx('00101', gross='115.0', tare='15.00'))
    assert a == b and a['gross_kg'] == '115'


def test_canonical_rejects_missing_weighing_evidence():
    canon, why = canonical(tx('00101', tare=None))
    assert canon is None and '缺少称重依据' in why


def test_canonical_rejects_negative_net_and_bad_rate():
    assert canonical(tx('00101', gross='10', tare='20'))[0] is None
    assert canonical(tx('00101', rate='1.5'))[0] is None


def test_settle_matches_auction_formula():
    line = settle(canonical(tx('00101'))[0])
    assert line['net_kg'] == '100' and line['billable_kg'] == '95'
    assert line['amount'] == '1168.50' and line['due'] == '1168.50'


def test_identical_cross_terminal_echo_merges_once():
    rows = [{'terminal_id': 'T1', 'seq': 1, 'payload': tx('00101')},
            {'terminal_id': 'T2', 'seq': 3, 'payload': tx('00101', gross='115.0')}]
    verdict = derive_ticket('00101', rows)
    assert verdict['state'] == 'merged'
    assert verdict['line']['terminals'] == ['T1', 'T2']
    assert verdict['line']['copies'] == 2


def test_conflicting_content_is_held_not_merged():
    rows = [{'terminal_id': 'T1', 'seq': 1, 'payload': tx('00101')},
            {'terminal_id': 'T2', 'seq': 1, 'payload': tx('00101', gross='118')}]
    verdict = derive_ticket('00101', rows)
    assert verdict['state'] == 'held' and verdict['hold']['code'] == 'TICKET_CONFLICT'


def test_missing_evidence_held_then_supplemented():
    broken = [{'terminal_id': 'T1', 'seq': 1, 'payload': tx('00101', tare=None)}]
    assert derive_ticket('00101', broken)['hold']['code'] == 'MISSING_EVIDENCE'
    fixed = broken + [{'terminal_id': 'T1', 'seq': 2, 'payload': tx('00101')}]
    assert derive_ticket('00101', fixed)['state'] == 'merged'


def test_watermark_tracks_contiguous_prefix():
    assert watermark([1, 2, 4]) == {'watermark': 2, 'max_seen': 4, 'gaps': [3]}
    assert watermark([2, 3])['watermark'] == 0
    assert watermark([]) == {'watermark': 0, 'max_seen': 0, 'gaps': []}


def test_delta_added_withdrawn_changed():
    old = [{'ticket': 'a', 'amount': '10.00'}, {'ticket': 'b', 'amount': '20.00'},
           {'ticket': 'c', 'amount': '30.00'}]
    new = [{'ticket': 'b', 'amount': '20.00'}, {'ticket': 'c', 'amount': '35.00'},
           {'ticket': 'd', 'amount': '40.00'}]
    delta = compute_delta(old, new)
    assert delta['added'] == '75.00' and delta['withdrawn'] == '40.00'
    assert delta['added_tickets'] == ['c', 'd'] and delta['withdrawn_tickets'] == ['a', 'c']


# ---------- 存储：幂等与证据 ----------

def test_retry_same_batch_adds_no_transactions(db):
    first = session_store.ingest(db, 'S1', 'T1', 1, [tx('00101')])
    second = session_store.ingest(db, 'S1', 'T1', 1, [tx('00101')])
    assert first['status'] == 'applied' and second['status'] == 'duplicate'
    assert len(session_store.evidence(db, 'S1')) == 1
    assert session_store.state(db, 'S1')['total'] == '1168.50'


def test_same_seq_different_content_rejected_and_evidence_kept(db):
    session_store.ingest(db, 'S1', 'T1', 1, [tx('00101')])
    with pytest.raises(BusinessError) as err:
        session_store.ingest(db, 'S1', 'T1', 1, [tx('00999')])
    assert err.value.code == 'SEQ_CONFLICT' and err.value.status == 409
    kept = session_store.evidence(db, 'S1')
    assert len(kept) == 1 and kept[0]['ticket'] == '00101'


def test_bad_upload_rejected(db):
    with pytest.raises(BusinessError):
        session_store.ingest(db, 'S1', 'T1', 0, [tx('00101')])
    with pytest.raises(BusinessError):
        session_store.ingest(db, 'S1', 'T1', 1, [])


def test_cross_terminal_conflict_withdraws_on_next_publish(db):
    session_store.ingest(db, 'S1', 'T1', 1, [tx('00101'), tx('00102', gross='110', tare='10')])
    v1 = session_store.publish(db, 'S1')
    assert v1['version'] == 1 and v1['total'] == '2337.00'
    session_store.ingest(db, 'S1', 'T2', 1, [tx('00101', gross='118')])
    state = session_store.state(db, 'S1')
    assert [h['ticket'] for h in state['held']] == ['00101']
    assert state['unpublished_changes']
    v2 = session_store.publish(db, 'S1')
    assert v2['version'] == 2 and v2['total'] == '1168.50'
    assert v2['added'] == '0.00' and v2['withdrawn'] == '1168.50'
    assert v2['delta']['withdrawn_tickets'] == ['00101']
    kept = session_store.evidence(db, 'S1', ticket='00101')
    assert len(kept) == 2  # 冲突双方原始记录都保留


def test_missing_evidence_held_until_supplemented(db):
    session_store.ingest(db, 'S1', 'T1', 1, [tx('00101', tare=None)])
    state = session_store.state(db, 'S1')
    assert state['held'][0]['code'] == 'MISSING_EVIDENCE' and state['payable'] == []
    session_store.ingest(db, 'S1', 'T1', 2, [tx('00101')])
    v1 = session_store.publish(db, 'S1')
    assert v1['added'] == '1168.50' and v1['delta']['added_tickets'] == ['00101']
    assert len(session_store.evidence(db, 'S1', ticket='00101')) == 2  # 缺证原始记录未丢


def test_publish_failure_keeps_old_version(db, monkeypatch):
    session_store.ingest(db, 'S1', 'T1', 1, [tx('00101')])
    v1 = session_store.publish(db, 'S1')
    session_store.ingest(db, 'S1', 'T1', 2, [tx('00102', gross='110', tare='10')])

    def boom(rows):
        raise RuntimeError('模拟发布中途失败')

    monkeypatch.setattr(session_store, 'derive_session', boom)
    with pytest.raises(RuntimeError):
        session_store.publish(db, 'S1')
    monkeypatch.undo()
    current = session_store.get_publication(db, 'S1')
    assert current['version'] == v1['version'] and current['seal'] == v1['seal']
    assert len(session_store.state(db, 'S1')['publications']) == 1


def test_publish_version_conflict_409_and_unchanged(db):
    session_store.ingest(db, 'S1', 'T1', 1, [tx('00101')])
    session_store.publish(db, 'S1')
    with pytest.raises(BusinessError) as err:
        session_store.publish(db, 'S1', expect_version=9)
    assert err.value.status == 409
    again = session_store.publish(db, 'S1')
    assert again['unchanged'] and again['version'] == 1
    assert len(session_store.state(db, 'S1')['publications']) == 1


def test_publish_empty_session_rejected(db):
    with pytest.raises(BusinessError) as err:
        session_store.publish(db, 'S1')
    assert err.value.code == 'NOTHING_TO_PUBLISH'


def test_watermark_gap_visible_in_state(db):
    session_store.ingest(db, 'S1', 'T1', 1, [tx('00101')])
    session_store.ingest(db, 'S1', 'T1', 3, [tx('00103', gross='100', tare='0')])
    terminal = session_store.state(db, 'S1')['terminals'][0]
    assert terminal['watermark'] == 1 and terminal['gaps'] == [2]


# ---------- HTTP 层 ----------

def test_session_page_and_api_flow(client):
    assert client.get('/session').status_code == 200
    assert client.post('/api/session/demo').status_code == 200
    page = client.get('/session?session_id=evening-demo')
    assert page.status_code == 200
    for label in ('水位', '待补票', '本次新增', '本次撤回', '原始成交与称重证据'):
        assert label in page.text
    state = client.get('/api/session/state?session_id=evening-demo').json()
    assert state['current']['version'] == 2
    assert state['current']['added'] == '2337.00' and state['current']['withdrawn'] == '1168.50'
    assert {h['ticket'] for h in state['held']} == {'00101'}


def test_demo_seed_is_idempotent(client):
    client.post('/api/session/demo')
    client.post('/api/session/demo')
    state = client.get('/api/session/state?session_id=evening-demo').json()
    assert len(state['publications']) == 2
    assert state['raw_count'] == 8


def test_upload_api_idempotent_retry(client):
    payload = {'session_id': 'S9', 'terminal_id': 'T1', 'seq': 1, 'transactions': [tx('00101')]}
    first = client.post('/api/session/uploads', json=payload).json()
    second = client.post('/api/session/uploads', json=payload).json()
    assert first['status'] == 'applied' and second['status'] == 'duplicate'
    assert second['state']['raw_count'] == 1
    conflict = client.post('/api/session/uploads',
                           json={**payload, 'transactions': [tx('00777')]} )
    assert conflict.status_code == 409


def test_publish_api_expect_version(client):
    client.post('/api/session/uploads',
                json={'session_id': 'S8', 'terminal_id': 'T1', 'seq': 1, 'transactions': [tx('00101')]})
    assert client.post('/api/session/publish', json={'session_id': 'S8', 'expect_version': 5}).status_code == 409
    ok = client.post('/api/session/publish', json={'session_id': 'S8', 'expect_version': 0})
    assert ok.status_code == 200 and ok.json()['version'] == 1
    evidence = client.get('/api/session/evidence?session_id=S8').json()
    assert len(evidence) == 1 and 'gross_kg' in evidence[0]['payload']
