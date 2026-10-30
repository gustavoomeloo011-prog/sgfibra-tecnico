"""Administrator-controlled maintenance, off until explicitly enabled."""
import os
import time
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from app.operations import cache_snapshot, save_cache_snapshot, role_for
from app_fastapi.state import store
import asyncio

KEY = 'application-maintenance-v1'

async def is_owner(request):
    session = await store.get_session(request.cookies.get('ebf_session', ''))
    user = (session or {}).get('user', {})
    username = str(user.get('usuario') or '')
    return bool(username and username.casefold() == os.getenv('MAINTENANCE_OWNER', 'Gustavo').strip().casefold()
                and await asyncio.to_thread(role_for, username) == 'superadmin')

def install(app):
    @app.middleware('http')
    async def guard(request, call_next):
        path = request.url.path
        if path.startswith('/api/') and path not in ('/api/maintenance', '/api/login', '/api/logout'):
            saved = await asyncio.to_thread(cache_snapshot, KEY)
            if (saved or {}).get('enabled') and not await is_owner(request):
                return JSONResponse({'error':'Site em manutenção. Tente novamente mais tarde.', 'maintenance':True}, status_code=503, headers={'Cache-Control':'no-store'})
        return await call_next(request)

    @app.get('/api/maintenance')
    async def status(request: Request):
        saved = await asyncio.to_thread(cache_snapshot, KEY) or {}
        return JSONResponse({'enabled':bool(saved.get('enabled')), 'can_manage':await is_owner(request)}, headers={'Cache-Control':'no-store'})

    @app.post('/api/maintenance')
    async def update(request: Request):
        if not await is_owner(request):
            raise HTTPException(403, 'Somente o administrador autorizado pode alterar a manutenção.')
        # Browser writes must originate from this application.
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            from urllib.parse import urlsplit
            if urlsplit(origin).netloc != request.headers.get('host'):
                raise HTTPException(403, 'Origem inválida.')
        body = await request.json()
        if type(body.get('enabled')) is not bool:
            raise HTTPException(400, 'Informe enabled como booleano.')
        await asyncio.to_thread(save_cache_snapshot, KEY, {'enabled':body['enabled'], 'updated_at':time.time(), 'actor':os.getenv('MAINTENANCE_OWNER','Gustavo')})
        return {'enabled':body['enabled'], 'can_manage':True}
