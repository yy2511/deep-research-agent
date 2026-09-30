"""自定义连接与模型资料：离线 HTTP + SDK 接线，不读取真实密钥。"""
from types import SimpleNamespace
from unittest.mock import MagicMock
import json
import pytest
from fastapi.testclient import TestClient
from dra import llm, runtime_config, web


@pytest.fixture
def registry(tmp_path, monkeypatch):
    from dra import provider_registry as pr
    monkeypatch.setattr(pr, '_PATH', tmp_path/'providers.json')
    monkeypatch.setattr(pr, '_CATALOG_PATH', tmp_path/'catalog.json')
    monkeypatch.setattr(llm, '_ENV_PATH', tmp_path/'missing.env')
    monkeypatch.setattr(llm, '_clients', {})
    for key,base in llm._PROVIDERS.values():
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(base, raising=False)
    monkeypatch.setattr(runtime_config, '_CONFIG_PATH', tmp_path/'runtime.json')
    monkeypatch.setattr(runtime_config, '_cache', None)
    monkeypatch.setattr(web.REGISTRY, 'active_count', lambda: 0)
    return pr, TestClient(web.app)


def create(client, **patch):
    response=client.post('/api/providers',json={
        'label':'自定义测试','base_url':'https://gateway.example/v1','api_key':'secret-test-key',**patch})
    assert response.status_code==200,response.text
    assert 'secret-test-key' not in response.text
    return response.json()


def test_connection_create_update_delete_and_key_never_returned(registry):
    pr,client=registry
    item=create(client); pid=item['name']
    assert item['custom'] and item['key_configured']
    assert pr._PATH.stat().st_mode & 0o777 == 0o600
    assert 'secret-test-key' not in client.get('/api/providers').text
    assert pr.credentials(pid)==('secret-test-key','https://gateway.example/v1')
    response=client.put(f'/api/providers/{pid}',json={
        'label':'改名','base_url':'https://gateway.example/v1','api_key':''})
    assert response.status_code==200,response.text
    assert pr.credentials(pid)[0]=='secret-test-key'
    assert client.delete(f'/api/providers/{pid}').status_code==200
    assert pid not in [p['name'] for p in client.get('/api/providers').json()]
    assert 'secret-test-key' not in pr._PATH.read_text()


def test_metadata_precedence_and_unknown_efforts_are_not_invented(registry,monkeypatch):
    pr,client=registry
    pid=create(client,catalog_provider='example')['name']
    pr._CATALOG_PATH.write_text(json.dumps({'example':{'api':'https://gateway.example/v1','models':{
        'test-model':{'limit':{'context':200000,'output':16000},'reasoning':True,
                      'reasoning_options':[{'type':'effort','values':['low','high','xhigh']}]}}}}))
    fake=MagicMock()
    fake.models.list.return_value=SimpleNamespace(data=[SimpleNamespace(model_dump=lambda:{
        'id':'test-model','context_length':128000}),SimpleNamespace(model_dump=lambda:{'id':'unknown'})])
    monkeypatch.setattr(llm,'_get_client',lambda p:fake)
    response=client.post(f'/api/providers/{pid}/refresh')
    assert response.status_code==200,response.text
    models={m['id']:m for m in response.json()['model_details']}
    assert models['test-model']['context_window']==128000
    assert models['test-model']['sources']['context_window']=='api'
    assert models['test-model']['reasoning_efforts']==['low','high','xhigh']
    assert models['unknown']['context_window'] is None
    assert models['unknown']['reasoning_efforts'] is None
    assert client.put(f'/api/providers/{pid}/model-metadata',json={
        'model':'test-model','context_window':64000,'reasoning_efforts':['high']}).status_code==200
    status=next(p for p in client.get('/api/providers').json() if p['name']==pid)
    model=status['model_details'][0]
    assert model['context_window']==64000 and model['reasoning_efforts']==['high']
    assert model['sources']['context_window']=='manual'


def test_custom_connection_reaches_sdk_and_reasoning_wire(registry,monkeypatch):
    pr,client=registry
    item=create(client,reasoning_format='openrouter');pid=item['name']
    constructor=MagicMock()
    monkeypatch.setattr(llm,'OpenAI',constructor)
    llm._get_client(pid)
    assert constructor.call_args.kwargs['api_key']=='secret-test-key'
    assert constructor.call_args.kwargs['base_url']=='https://gateway.example/v1'
    assert llm._reasoning_extra_body(provider=pid,reasoning=True,effort='xhigh')=={'reasoning':{'effort':'xhigh'}}


def test_used_connection_and_running_research_block_delete(registry,monkeypatch):
    _,client=registry
    pid=create(client)['name']
    config=runtime_config.current().model_copy(deep=True)
    config.subagent.provider=pid
    runtime_config.save(config)
    assert client.delete(f'/api/providers/{pid}').status_code==409
    monkeypatch.setattr(web.REGISTRY,'active_count',lambda:1)
    assert client.put(f'/api/providers/{pid}',json={
        'label':'change','base_url':'https://elsewhere.example/v1','api_key':'new-secret'}).status_code==409


def test_validation_and_upstream_errors_do_not_echo_keys_and_failure_keeps_models(registry,monkeypatch):
    _,client=registry
    response=client.post('/api/providers',json={'label':'test','base_url':'bad','api_key':'DO-NOT-ECHO-THIS'})
    assert response.status_code==400 and 'DO-NOT-ECHO-THIS' not in response.text
    pid=create(client)['name']
    fake=MagicMock()
    fake.models.list.return_value=SimpleNamespace(data=[{'id':'existing','reasoning':True}])
    monkeypatch.setattr(llm,'_get_client',lambda p:fake)
    first=client.post(f'/api/providers/{pid}/refresh').json()
    assert first['model_details'][0]['reasoning_efforts'] is None
    fake.models.list.side_effect=RuntimeError('upstream reflected secret-test-key')
    response=client.post(f'/api/providers/{pid}/refresh')
    assert 'secret-test-key' not in response.text
    assert response.json()['models']==['existing']
    assert not response.json()['reachable']


def test_catalog_does_not_infer_gateway_limits_from_model_name(registry):
    pr,client=registry
    pr._CATALOG_PATH.write_text(json.dumps({'openai':{'api':'https://api.openai.com/v1','models':{
        'gpt-test':{'limit':{'context':200000},'reasoning_options':[{'type':'effort','values':['high']}]}}}}))
    pid=create(client)['name']
    # 用手工记录让 ID 出现在列表；上下文、档位不能跨服务商自动借用。
    response=client.put(f'/api/providers/{pid}/model-metadata',json={'model':'gpt-test','context_window':64000})
    model=response.json()['model_details'][0]
    assert model['context_window']==64000 and model['reasoning_efforts'] is None
    assert model['sources']=={'context_window':'manual'}


def test_endpoint_change_during_discovery_cannot_install_old_models(registry,monkeypatch):
    pr,client=registry
    pid=create(client)['name']
    fake=MagicMock()
    def change(**kwargs):
        pr.save_connection(pr.ConnectionInput(label='changed',base_url='https://new.example/v1'),pid)
        return SimpleNamespace(data=[{'id':'stale-model'}])
    fake.models.list.side_effect=change
    monkeypatch.setattr(llm,'_get_client',lambda p:fake)
    assert client.post(f'/api/providers/{pid}/refresh').status_code==400
    assert pr.status(pid).models==[]


def test_real_sdk_discovery_and_chat_use_custom_connection(registry,monkeypatch):
    import httpx
    from openai import OpenAI
    _,client=registry
    pid=create(client,reasoning_format='thinking')['name']
    captured=[]
    def serve(request):
        captured.append(request)
        assert request.headers['authorization']=='Bearer secret-test-key'
        if request.url.path.endswith('/models'):
            return httpx.Response(200,json={'object':'list','data':[{'id':'m','context_window':96000,'reasoning_efforts':['high','max']}]})
        return httpx.Response(200,json={'id':'answer','object':'chat.completion','model':'m','choices':[
            {'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'OK'}}]})
    transport=httpx.MockTransport(serve)
    monkeypatch.setattr(llm,'OpenAI',lambda **kw:OpenAI(**kw,http_client=httpx.Client(transport=transport)))
    models=client.post(f'/api/providers/{pid}/refresh').json()['model_details']
    assert models[0]['context_window']==96000
    assert models[0]['reasoning_efforts']==['high','max']
    llm.chat([{'role':'user','content':'test'}],model='m',provider=pid,reasoning=True,effort='max')
    llm.call_tools([{'role':'user','content':'test'}],model='m',provider=pid,reasoning=True,effort='high',tools=[])
    for request in captured[1:]:
        body=json.loads(request.content)
        assert body['thinking']=={'type':'enabled'}
        assert body['reasoning_effort'] in ('max','high')
        assert str(request.url)=='https://gateway.example/v1/chat/completions'


@pytest.fixture
def discovery_sdk(registry, monkeypatch):
    """走真实 SDK 的异常包装，HTTP 用离线传输替代。"""
    import httpx
    from openai import OpenAI
    pr, _ = registry
    clients = []
    monkeypatch.setattr(pr.time, 'sleep', lambda _: None)

    def install(handler):
        def construct(**kwargs):
            sdk = OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
            assert sdk.max_retries == 0
            clients.append(sdk)
            return sdk
        monkeypatch.setattr(llm, 'OpenAI', construct)
        return clients

    yield install
    for sdk in clients:
        sdk.close()


@pytest.mark.parametrize('failure', ['connect', 'timeout'])
def test_model_discovery_retries_network_failure_with_new_client(registry, discovery_sdk, failure):
    import httpx
    _, client = registry
    pid = create(client)['name']
    requests = []

    def serve(request):
        requests.append(request)
        if len(requests) == 1:
            error = httpx.ConnectError if failure == 'connect' else httpx.ReadTimeout
            raise error('transient network failure', request=request)
        return httpx.Response(200, json={'data': [{'id': 'recovered'}]})

    clients = discovery_sdk(serve)
    response = client.post(f'/api/providers/{pid}/refresh').json()
    assert response['reachable'] is True
    assert response['models'] == ['recovered'] and response['error'] is None
    assert len(requests) == len(clients) == 2
    assert all(set(req.extensions['timeout'].values()) == {10} for req in requests)


def test_model_discovery_tls_retry_exhaustion_keeps_models_and_hides_secrets(registry, discovery_sdk):
    import httpx
    import ssl
    _, client = registry
    pid = create(client)['name']
    attempts = []

    def serve(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(200, json={'data': [{'id': 'existing'}]})
        cause = ssl.SSLEOFError(8, 'unexpected EOF secret-test-key')
        raise httpx.ConnectError(str(cause), request=request) from cause

    discovery_sdk(serve)
    assert client.post(f'/api/providers/{pid}/refresh').json()['reachable']
    response = client.post(f'/api/providers/{pid}/refresh')
    assert len(attempts) == 3  # 首次成功 + 失败请求及一次重试。
    assert response.json()['models'] == ['existing']
    assert response.json()['reachable'] is False
    assert 'TLS' in response.json()['error']
    assert 'secret-test-key' not in response.text
    assert 'unexpected EOF' not in response.text


@pytest.mark.parametrize('status_code', [401, 403, 404, 429, 500])
def test_model_discovery_does_not_retry_http_errors(registry, discovery_sdk, status_code):
    import httpx
    _, client = registry
    pid = create(client)['name']
    attempts = []

    def serve(request):
        attempts.append(request)
        return httpx.Response(status_code, json={'error': {'message': 'secret-test-key connection failed'}})

    discovery_sdk(serve)
    response = client.post(f'/api/providers/{pid}/refresh')
    assert len(attempts) == 1
    assert response.json()['reachable'] is False
    assert str(status_code) in response.json()['error']
    assert 'secret-test-key' not in response.text


@pytest.mark.parametrize(('error_type', 'expected'), [
    ('ProxyError', '代理'), ('ConnectError', '网络连接'), ('ReadTimeout', '超时'),
])
def test_model_discovery_classifies_wrapped_transport_errors(registry, discovery_sdk, error_type, expected):
    import httpx
    _, client = registry
    pid = create(client)['name']

    def serve(request):
        raise getattr(httpx, error_type)('secret-test-key', request=request)

    discovery_sdk(serve)
    response = client.post(f'/api/providers/{pid}/refresh')
    assert expected in response.json()['error']
    assert '密钥' not in response.json()['error']
    assert 'secret-test-key' not in response.text


@pytest.mark.parametrize('mode', ['chat', 'tools'])
def test_custom_provider_html_is_protocol_error_without_retry(registry, discovery_sdk, mode):
    import httpx
    _, client = registry
    pid = create(client)['name']
    attempts = []

    def serve(request):
        attempts.append(request)
        return httpx.Response(200, text='<html>secret-test-key gateway home</html>',
                              headers={'content-type': 'text/html'})

    discovery_sdk(serve)
    kwargs = {'provider': pid, 'model': 'm', 'max_retries': 2}
    with pytest.raises(llm.LLMProtocolError, match=r'Base URL.*?/v1') as caught:
        if mode == 'chat':
            llm.chat([{'role': 'user', 'content': 'test'}], **kwargs)
        else:
            llm.call_tools([{'role': 'user', 'content': 'test'}], tools=[], **kwargs)
    assert len(attempts) == 1
    assert 'secret-test-key' not in str(caught.value)


def test_config_test_reports_wrong_chat_path_and_recovers_after_url_edit(registry, discovery_sdk):
    import httpx
    _, client = registry
    pid = create(client, base_url='https://gateway.example')['name']

    def serve(request):
        assert request.headers['authorization'] == 'Bearer secret-test-key'
        if request.url.path.endswith('/models'):
            return httpx.Response(200, json={'data': [{'id': 'm'}]})
        if request.url.path == '/v1/chat/completions':
            return httpx.Response(200, json={'id': 'answer', 'object': 'chat.completion', 'model': 'm',
                'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'OK'}}]})
        return httpx.Response(200, text='<html>secret-test-key gateway home</html>',
                              headers={'content-type': 'text/html'})

    discovery_sdk(serve)
    # 列表成功不代表根地址也提供聊天接口。
    assert client.post(f'/api/providers/{pid}/refresh').json()['reachable']
    body = {'model': 'm', 'provider': pid, 'reasoning': True, 'effort': None}
    response = client.post('/api/config/test', json=body)
    assert response.json()['ok'] is False
    assert 'Base URL' in response.json()['error'] and '/v1' in response.json()['error']
    assert '密钥' not in response.json()['error'] and 'secret-test-key' not in response.text
    assert client.put(f'/api/providers/{pid}', json={
        'label': '自定义测试', 'base_url': 'https://gateway.example/v1', 'api_key': '',
    }).status_code == 200
    assert client.post('/api/config/test', json=body).json() == {'ok': True}


@pytest.mark.parametrize('status_code', [400, 401, 403, 404, 429, 500])
def test_config_test_keeps_wrapped_http_status_without_leaking_body(registry, discovery_sdk, status_code):
    import httpx
    _, client = registry
    pid = create(client)['name']

    def serve(request):
        return httpx.Response(status_code, json={'error': {'message': 'secret-test-key upstream detail'}})

    discovery_sdk(serve)
    response = client.post('/api/config/test', json={'model': 'm', 'provider': pid})
    assert response.json()['ok'] is False
    assert f'HTTP {status_code}' in response.json()['error']
    assert '模型列表' not in response.json()['error']
    assert 'secret-test-key' not in response.text and 'upstream detail' not in response.text
