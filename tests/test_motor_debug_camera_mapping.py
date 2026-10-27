"""Actual debug HTTP routes: mapping uses Fleet and never contacts the arm."""
from http.server import ThreadingHTTPServer
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request,urlopen

import pytest
from scripts import motor_debug_server as debug


@pytest.fixture
def console(monkeypatch,tmp_path):
    calls=[]
    def proxy(method,path,body=None):
        calls.append((method,path,body))
        return 200,{'camera_options':[], '说明':'对应关系已保存，请人工重新校准'}
    monkeypatch.setattr(debug,'_agent_passthrough',proxy)
    monkeypatch.setattr(debug,'DATA_DIR',tmp_path)
    monkeypatch.setattr(debug,'daemon_for',lambda *_: pytest.fail('映射入口不得接触机械臂'))
    monkeypatch.setattr(debug,'require_context',lambda *_args,**_kw: pytest.fail('映射入口不要求旧画面上下文'))
    server=ThreadingHTTPServer(('127.0.0.1',0),debug.Handler)
    origin=f'http://127.0.0.1:{server.server_port}'
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    with urlopen(origin+'/api/debug-session') as response:
        csrf=json.load(response)['csrf_token'];cookie=response.headers['Set-Cookie'].split(';')[0]
    def call(path,body=None,csrf_enabled=True):
        headers={'Content-Type':'application/json','Origin':origin,'Sec-Fetch-Site':'same-origin','Cookie':cookie}
        if csrf_enabled:headers['X-Debug-CSRF']=csrf
        request=Request(origin+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        try:
            with urlopen(request,timeout=3) as response:return response.status,json.load(response)
        except HTTPError as error:return error.code,json.load(error)
    yield call,calls
    server.shutdown();thread.join(timeout=3);server.server_close()


def test_mapping_reads_options_and_forwards_explicit_channel_without_old_context(console):
    call,calls=console
    assert call('/api/camera-options?device=arm-01')[0]==200
    assert calls[-1][:2]==('GET','/v1/devices/arm-01/binding/options')
    assert call('/api/camera-bind',{'device':'arm-01','cameraResource':'camera:cam-09','requestId':'save-1'})[0]==200
    assert calls[-1][:2]==('PUT','/v1/devices/arm-01/binding')
    assert calls[-1][2]['camera_binding_id']=='camera:cam-09'
    assert calls[-1][2]['client_request_id']=='save-1'


def test_mapping_rejects_missing_csrf_and_invalid_channel(console):
    call,calls=console
    body={'device':'arm-01','cameraResource':'camera:cam-09','requestId':'save-1'}
    assert call('/api/camera-bind',body,csrf_enabled=False)[0]==403
    assert call('/api/camera-bind',{**body,'cameraResource':'http://attacker/'})[0]==400
    assert call('/api/camera-bind',{**body,'device':''})[0]==400
    assert not calls


def test_mapping_preserves_upstream_error_without_local_mutation(console,monkeypatch):
    call,calls=console
    monkeypatch.setattr(debug,'_agent_passthrough',lambda *_args: (502,{'error':{'key':'upstream_error','title':'平台不可达'}}))
    status,payload=call('/api/camera-bind',{'device':'arm-01','cameraResource':'camera:cam-09','requestId':'save-1'})
    assert status==502 and payload['error']['key']=='upstream_error'
    assert not calls
