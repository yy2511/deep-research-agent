"""用户 API 连接与模型资料；密钥只在后端读取，公开目录不接收凭据。"""
from __future__ import annotations

import json
import re
import ssl
import threading
import time
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from openai import APIConnectionError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from dra.paths import DATA_DIR

_PATH = DATA_DIR / 'provider_connections.json'
_CATALOG_PATH = DATA_DIR / 'model_catalog.json'
_LOCK = threading.RLock()
_CATALOG_LOCK = threading.Lock()
CATALOG_URL = 'https://models.dev/api.json'


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=80)
    base_url: str = Field(max_length=1000)
    api_key: SecretStr | None = None
    catalog_provider: str = Field(default='', max_length=100, pattern=r'^[\w.-]*$')
    reasoning_format: Literal['auto','standard','thinking','openrouter','none'] = 'auto'

    @field_validator('base_url')
    @classmethod
    def valid_url(cls, value):
        url=urlsplit(value)
        if (url.scheme not in ('http','https') or not url.hostname or url.username or url.password
                or url.query or url.fragment or any(c.isspace() for c in value)):
            raise ValueError('Base URL 须为 http(s) 地址，不含账号、查询参数或片段')
        if url.path.rstrip('/').endswith(('/chat/completions','/responses','/models')):
            raise ValueError('请填写 API 根地址（通常以 /v1 结尾），不要填写具体接口路径')
        return value.rstrip('/')

    @field_validator('api_key')
    @classmethod
    def valid_key(cls, value):
        if value is not None:
            key=value.get_secret_value().strip()
            if len(key)>4096 or any(c.isspace() for c in key):
                raise ValueError('API Key 长度或格式不正确')
            return SecretStr(key)
        return value


class ModelOverride(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    model: str = Field(min_length=1, max_length=300)
    context_window: int | None = Field(default=None, gt=0, le=100_000_000)
    max_input_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    image_input: bool | None = None
    image_transport: bool | None = None
    reasoning_efforts: list[str] | None = Field(default=None, max_length=16)

    @field_validator('reasoning_efforts')
    @classmethod
    def valid_efforts(cls, value):
        if value is not None and any(not re.fullmatch(r'[a-zA-Z0-9_-]{1,32}',x) for x in value):
            raise ValueError('思考档位须为简短英文名称，用逗号分隔')
        return list(dict.fromkeys(value)) if value is not None else None


def _load():
    if not _PATH.exists():
        return {'schema_version':1,'connections':{},'discovery':{},'overrides':{}}
    try:
        data=json.loads(_PATH.read_text())
        if data.get('schema_version')!=1 or any(not isinstance(data.get(k),dict) for k in ('connections','discovery','overrides')):
            raise ValueError()
        return data
    except (ValueError,TypeError,AttributeError) as exc:
        raise ValueError('API 连接配置无法读取，请检查本地配置文件') from exc


def _save(data):
    from dra.durable import atomic_json
    atomic_json(_PATH,data)  # NamedTemporaryFile 创建时为 0600，原子替换后保持。


def custom_connections():
    with _LOCK:
        return _load()['connections']


def exists(provider):
    from dra import llm
    return provider in llm._PROVIDERS or provider in custom_connections()


def credentials(provider):
    from dra import llm
    if provider in llm._PROVIDERS:
        llm.load_dotenv(llm._ENV_PATH,override=True)
        return llm._env_value(llm._PROVIDERS[provider][0]) or '', llm._resolve_base_url(provider)
    row=custom_connections().get(provider)
    if row is None:
        raise ValueError('API 连接不存在')
    return row['api_key'],row['base_url']


def save_connection(payload: ConnectionInput, provider=None):
    from dra import llm
    with _LOCK:
        data=_load()
        if provider is not None and provider not in data['connections']:
            raise ValueError('只能编辑自定义连接；环境变量连接在 .env 中管理')
        old=data['connections'].get(provider,{})
        key=(payload.api_key.get_secret_value() if payload.api_key else '') or old.get('api_key','')
        if not key:
            raise ValueError('新增连接需要填写 API Key')
        if len(data['connections'])>=30 and provider is None:
            raise ValueError('最多保存 30 条自定义连接')
        provider=provider or 'custom_'+uuid4().hex[:16]
        row=payload.model_dump(exclude={'api_key'});row['api_key']=key
        data['connections'][provider]=row
        data['discovery'].pop(provider,None)
        if old and (old['base_url']!=row['base_url'] or old.get('catalog_provider','')!=row['catalog_provider']):
            data['overrides'].pop(provider,None)
        _save(data)
        llm._discard_client(provider)
    return status(provider)


def delete_connection(provider):
    from dra import llm
    with _LOCK:
        data=_load()
        if provider not in data['connections']:
            raise ValueError('只能删除自定义连接；环境变量连接在 .env 中管理')
        for field in ('connections','discovery','overrides'):
            data[field].pop(provider,None)
        _save(data)
        llm._discard_client(provider)


def reasoning_format(provider):
    row=custom_connections().get(provider)
    if row is None:return None  # 原内置连接保持已有协议行为。
    profile=row['reasoning_format']
    if profile!='auto':return profile
    host=urlsplit(row['base_url']).hostname
    if host in ('opencode.ai','api.deepseek.com'):return 'thinking'
    if host=='openrouter.ai':return 'openrouter'
    return 'standard'


def _positive(value):
    return value if type(value) is int and 0<value<=100_000_000 else None


def model_metadata(row):
    """只解释明确字段；不按模型名字猜窗口/档位，不把参数名变成枚举。"""
    limit=row.get('limit') if isinstance(row.get('limit'),dict) else {}
    top=row.get('top_provider') if isinstance(row.get('top_provider'),dict) else {}
    context=next((v for v in map(_positive,[row.get('context_length'),row.get('context_window'),
        row.get('max_context_length'),limit.get('context'),top.get('context_length')]) if v),None)
    output=next((v for v in map(_positive,[row.get('max_output_tokens'),row.get('max_completion_tokens'),limit.get('output'),
        top.get('max_completion_tokens')]) if v),None)
    input_limit=next((v for v in map(_positive,[row.get('max_input_tokens'),row.get('max_prompt_tokens'),limit.get('input')]) if v),None)
    efforts=row.get('reasoning_efforts',row.get('supported_reasoning_efforts'))
    options=row.get('reasoning_options')
    if efforts is None and isinstance(options,list):
        option=next((x for x in options if isinstance(x,dict) and x.get('type')=='effort'),None)
        if option is not None:efforts=option.get('values')
        elif not options:efforts=[]
    if not isinstance(efforts,list) or len(efforts)>16 or any(not isinstance(x,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,32}',x) for x in efforts):
        efforts=None
    reasoning=row.get('reasoning') if type(row.get('reasoning')) is bool else None
    if reasoning is None and efforts:reasoning=True
    modalities = row.get('input_modalities', (row.get('modalities') or {}).get('input') if isinstance(row.get('modalities'), dict) else None)
    architecture = row.get('architecture') or {}
    if modalities is None and isinstance(architecture, dict):
        modalities = architecture.get('input_modalities')
    image_input = row.get('image_input', row.get('supports_vision'))
    if type(image_input) is not bool:
        image_input = 'image' in modalities if isinstance(modalities, list) else None
    image_transport = row.get('image_transport') if type(row.get('image_transport')) is bool else None
    return {'image_input':image_input,'image_transport':image_transport,'context_window':context,'max_input_tokens':input_limit,'max_output_tokens':output,'reasoning':reasoning,
            'reasoning_efforts':list(dict.fromkeys(efforts)) if efforts is not None else None}


def _catalog():
    try:
        data=json.loads(_CATALOG_PATH.read_text())
        return data if isinstance(data,dict) else {}
    except (OSError,ValueError):return {}


def refresh_catalog(*,force=False):
    from dra.durable import atomic_json
    with _CATALOG_LOCK:
        if not force and _CATALOG_PATH.exists() and time.time()-_CATALOG_PATH.stat().st_mtime<86400:
            return {'ok':True,'cached':True}
        try:
            # 与服务商 Key 完全独立。限制下载体积，重定向不转发到其它目的地。
            with httpx.stream('GET',CATALOG_URL,timeout=8,follow_redirects=False) as response:
                response.raise_for_status();chunks=[];size=0
                for chunk in response.iter_bytes():
                    size+=len(chunk)
                    if size>16*1024*1024:raise ValueError('catalog too large')
                    chunks.append(chunk)
            data=json.loads(b''.join(chunks))
            if not isinstance(data,dict) or not any(isinstance(v,dict) and isinstance(v.get('models'),dict) for v in data.values()):
                raise ValueError('invalid catalog')
            # 只缓存本功能使用的字段，目录内容不作为指令或请求配置。
            catalog={}
            for key,item in data.items():
                if not isinstance(item,dict) or not isinstance(item.get('models'),dict):continue
                models={mid:model_metadata(m) for mid,m in item['models'].items() if isinstance(mid,str) and isinstance(m,dict)}
                catalog[key]={'api':item.get('api'),'models':models,'normalized':True}
            atomic_json(_CATALOG_PATH,catalog)
            return {'ok':True,'cached':False,'providers':len(catalog)}
        except (httpx.HTTPError,OSError,ValueError):
            return {'ok':False,'cached':bool(_catalog()),'error':'模型能力目录暂时无法更新；保留已有资料，未知字段可手动填写。'}


def _catalog_provider(base_url, explicit):
    data=_catalog()
    if explicit:return data.get(explicit,{})
    url=(base_url or 'https://api.openai.com/v1').rstrip('/')
    matches=[v for v in data.values() if isinstance(v,dict) and isinstance(v.get('api'),str) and v['api'].rstrip('/')==url]
    return matches[0] if len(matches)==1 else {}


def _public_url(value):
    if not value:return 'https://api.openai.com/v1'
    url=urlsplit(value)
    return urlunsplit((url.scheme,url.netloc.split('@')[-1],url.path,'',''))


def status(provider):
    from dra import llm
    with _LOCK:
        data=_load();custom=data['connections'].get(provider)
        key,base_url=credentials(provider)
        saved=data['discovery'].get(provider,{})
        overrides=data['overrides'].get(provider,{})
    catalog=_catalog_provider(base_url,(custom or {}).get('catalog_provider',''))
    details=[]
    for mid in dict.fromkeys([*saved.get('models',{}),*overrides]):
        candidate=catalog.get('models',{}).get(mid,{})
        public=candidate if catalog.get('normalized') else model_metadata(candidate)
        meta={'id':mid,**model_metadata({}),'sources':{}}
        for source,fields in (('catalog',public),('official', _official_images(base_url, mid)),('api',saved.get('models',{}).get(mid,{})),('manual',overrides.get(mid,{}))):
            for k in ('context_window','max_input_tokens','max_output_tokens','reasoning','reasoning_efforts','image_input','image_transport'):
                if fields.get(k) is not None:
                    meta[k]=fields[k];meta['sources'][k]=source
        details.append(meta)
    return llm.ProviderStatus(name=provider,label=(custom or {}).get('label',provider),custom=bool(custom),
        base_url=_public_url(base_url),key_configured=bool(key),reachable=saved.get('reachable',False),
        models=[m['id'] for m in details],model_details=details,error=saved.get('error'),
        checked_at=saved.get('checked_at'),catalog_provider=(custom or {}).get('catalog_provider',''),
        reasoning_format=(custom or {}).get('reasoning_format','auto'))


def list_status():
    from dra import llm
    return [status(p) for p in [*llm._PROVIDERS,*custom_connections()]]


def _official_images(base_url, model):
    # https://api-docs.deepseek.com/guides/vision/；只适用于实际官方端点。
    return ({'image_input': True, 'image_transport': True}
            if urlsplit(base_url or '').hostname == 'api.deepseek.com'
            and model in ('deepseek-flash', 'deepseek-v4-flash-vision-exp') else {})


def capacity_snapshot(provider, model):
    """Read cached route declarations only. A public catalog is not relay evidence.

    Explicit manual values can fill missing fields or lower a route limit; they
    cannot raise a smaller limit returned by that same endpoint. No network calls.
    """
    with _LOCK:
        data = _load()
        discovery = data['discovery'].get(provider, {})
        api = discovery.get('models', {}).get(model, {})
        manual = data['overrides'].get(provider, {}).get(model, {})
        connection = data['connections'].get(provider, {})
    result = {'provider': provider, 'model': model, 'checked_at': discovery.get('checked_at'),
              'protocol': 'chat_completions', 'reasoning_format': connection.get('reasoning_format', 'auto'),
              'sources': {}, 'declarations': {}}
    from dra import llm
    base_url = connection.get('base_url') or (llm._resolve_base_url(provider) if provider in llm._PROVIDERS else '')
    # 官方接口的明确能力不推广到同名中转模型；手动禁用和实际 API 声明优先。
    # https://api-docs.deepseek.com/guides/vision/
    official = _official_images(base_url, model)
    for field in ('image_input', 'image_transport'):
        values = {source: row[field] for source, row in (('official', official), ('api', api), ('manual', manual)) if type(row.get(field)) is bool}
        result[field] = next((row[field] for row in (manual, api, official) if type(row.get(field)) is bool), None)
        result['declarations'][field] = values
        result['sources'][field] = list(values)
    for field in ('context_window', 'max_input_tokens', 'max_output_tokens'):
        values = {source: row[field] for source, row in (('api', api), ('manual', manual))
                  if _positive(row.get(field)) is not None}
        result['declarations'][field] = values
        result[field] = min(values.values()) if values else None
        result['sources'][field] = [s for s, v in values.items() if v == result[field]]
    return result


def safe_error(exc, *, operation: Literal['models','chat']='models'):
    from dra.llm import LLMProtocolError
    # 调用层和 SDK 都可能包装原始错误；检查有界异常链，绝不回显异常原文。
    causes=[]
    cause=exc
    while cause is not None and len(causes)<6 and all(cause is not item for item in causes):
        causes.append(cause)
        cause=cause.__cause__ or cause.__context__
    code=next((item.status_code for item in causes
               if isinstance(getattr(item,'status_code',None),int)),None)
    if code==401:return '认证失败（HTTP 401），请检查 API Key。'
    if code==403:return '访问被拒绝（HTTP 403），请检查模型权限。'
    if code==404:
        if operation=='chat':return '模型调用接口或模型不存在（HTTP 404），请检查 Base URL 与模型 ID。'
        return '模型列表接口不存在（HTTP 404），可手动填写模型 ID。'
    if code in (400,422):return f'请求参数被拒绝（HTTP {code}），请检查模型 ID、参数与接口兼容性。'
    if code:return f'服务商请求失败（HTTP {code}），请检查地址与服务状态。'
    if any(isinstance(item,LLMProtocolError) for item in causes):
        return '模型接口返回网页或不兼容的响应，请检查 Base URL 是否指向 API 根路径（通常以 /v1 结尾）。'
    if any(isinstance(item,ssl.SSLError) for item in causes):
        return 'HTTPS 安全连接失败（TLS 握手异常），请稍后重试，或检查代理与上游服务。'
    if any(isinstance(item,httpx.ProxyError) for item in causes):
        return '代理连接失败，请检查代理是否可用后重试。'
    if any(isinstance(item,(TimeoutError,httpx.TimeoutException)) or 'timeout' in type(item).__name__.lower() for item in causes):
        return '连接超时，请稍后重试。'
    if any(isinstance(item,(APIConnectionError,httpx.TransportError)) for item in causes):
        return '网络连接失败，请稍后重试，或检查代理与服务状态。'
    return '请求失败，请检查地址、密钥或网络连接。'


def refresh_provider(provider):
    from dra import llm
    creds=credentials(provider)
    if not creds[0]:return status(provider)
    try:
        llm._discard_client(provider)
        for attempt in range(2):
            try:
                response=llm._get_client(provider).models.list(timeout=10)
                break
            except (APIConnectionError,httpx.TransportError):
                # 只读列表允许一次网络重试；重建连接，不叠加 SDK 内置重试。
                llm._discard_client(provider)
                if attempt:raise
                time.sleep(0.5)
        rows=response.data
        if not isinstance(rows,list) or len(rows)>4000:raise ValueError('invalid models')
        models={}
        for item in rows:
            row=item if isinstance(item,dict) else item.model_dump()
            if not isinstance(row,dict):row={'id':getattr(item,'id',None)}
            mid=row.get('id')
            if isinstance(mid,str) and 0<len(mid)<=300:models[mid]=model_metadata(row)
        saved={'models':models,'reachable':True,'checked_at':time.time(),'error':None}
    except Exception as exc:
        saved={'reachable':False,'checked_at':time.time(),'error':safe_error(exc)}
    with _LOCK:
        if credentials(provider)!=creds:raise ValueError('连接已修改，请重新获取模型')
        data=_load();previous=data['discovery'].get(provider,{})
        data['discovery'][provider]={**previous,**saved};_save(data)
    return status(provider)


def save_override(provider,override: ModelOverride):
    if not exists(provider):raise ValueError('API 连接不存在')
    with _LOCK:
        data=_load();models=data['overrides'].setdefault(provider,{})
        fields=override.model_dump(exclude={'model'},exclude_none=True)
        if fields:models[override.model]=fields
        else:models.pop(override.model,None)
        _save(data)
    return status(provider)
