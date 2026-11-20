"""晚场补传核对：页面与 API 路由。"""
from fastapi import APIRouter, Request, Body
from fastapi.templating import Jinja2Templates
from app.config import ROOT, META, RULE_VERSION
from app.errors import BusinessError
from app import session_store
from app.session_demo import seed_demo, DEMO_SESSION

router = APIRouter()
templates = Jinja2Templates(directory=str(ROOT / 'app/templates'))


def _db():
    from app.main import DB_PATH
    return DB_PATH


def _pick_session(session_id=None):
    if session_id:
        return session_id
    existing = session_store.sessions(_db())
    return existing[0]['session_id'] if existing else None


@router.get('/session')
def page(request: Request, session_id: str = None):
    picked = _pick_session(session_id)
    state = session_store.state(_db(), picked) if picked else {'exists': False, 'session_id': ''}
    return templates.TemplateResponse(request=request, name='session.html', context={
        'meta': META, 'state': state, 'sessions': session_store.sessions(_db()),
        'demo_session': DEMO_SESSION, 'rule_version': RULE_VERSION,
        'evidence': session_store.evidence(_db(), picked)[:200] if picked else [],
    })


@router.get('/api/session/state')
def api_state(session_id: str = None):
    picked = _pick_session(session_id)
    if not picked:
        return {'exists': False, 'session_id': ''}
    return session_store.state(_db(), picked)


@router.post('/api/session/uploads')
def api_upload(payload: dict = Body(...)):
    result = session_store.ingest(
        _db(), payload.get('session_id'), payload.get('terminal_id'),
        payload.get('seq'), payload.get('transactions'), payload.get('sent_at'))
    return result | {'state': session_store.state(_db(), result['session_id'])}


@router.post('/api/session/publish')
def api_publish(payload: dict = Body(...)):
    session_id = payload.get('session_id')
    if not session_id:
        raise BusinessError('BAD_UPLOAD', '场次编号不能为空')
    return session_store.publish(_db(), str(session_id).strip(), payload.get('expect_version'))


@router.get('/api/session/evidence')
def api_evidence(session_id: str, ticket: str = None):
    return session_store.evidence(_db(), session_id, ticket)


@router.post('/api/session/demo')
def api_demo():
    return seed_demo(_db())
