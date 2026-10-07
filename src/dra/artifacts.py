"""文件交稿：验证私有副本，正式结果最后引用；不从任意宿主路径交付。"""
from __future__ import annotations
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import tempfile
import threading
import time
from typing import Literal
from uuid import uuid4
import xml.etree.ElementTree as ET
from pydantic import BaseModel,ConfigDict,Field
from dra import llm
from dra.durable import atomic_json
from dra.execution import ExecutionError,workspace_path

OutputFormat=str
MIMES={'.html':('html','text/html'),'.pdf':('pdf','application/pdf'),'.png':('png','image/png'),
       '.jpg':('jpeg','image/jpeg'),'.jpeg':('jpeg','image/jpeg'),'.webp':('webp','image/webp'),'.gif':('gif','image/gif'),
       '.svg':('svg','image/svg+xml'),'.csv':('csv','text/csv'),'.json':('json','application/json'),
       '.md':('markdown','text/markdown'),'.txt':('text','text/plain'),
       **{e:('code','text/plain') for e in ('.py','.js','.ts','.sh','.sql','.r','.css')}}
_PUBLISH_LOCK=threading.Lock()


class ArtifactRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    path:str
    title:str=Field(min_length=1,max_length=200)
    kind:Literal['report','chart','mindmap','mindmap_source','data','code','other']='report'
    requirement_ids:list[str]=Field(default_factory=list,max_length=50)


def validate_file(filename,data):
    """已知预览格式检查完整性，其他文件原样下载，不用扩展名白名单挡交付。"""
    suffix=PurePosixPath(filename).suffix.lower()
    if suffix not in MIMES:
        return suffix.lstrip('.') or 'file', mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    fmt,mime=MIMES[suffix]
    try:
        if fmt=='pdf':
            # 不在 Web 进程展开 PDF 对象；浏览器负责预览。
            end=re.search(rb'startxref\s+(\d+)\s+%%EOF\s*$',data[-4096:])
            if not re.match(rb'%PDF-(1\.[0-7]|2\.0)\b',data[:16]) or not end:raise ValueError('PDF 文件头/尾无效')
            offset=int(end[1]);head=data[offset:offset+80]
            if not head.startswith(b'xref') and not re.match(rb'\d+\s+\d+\s+obj\b',head):raise ValueError('PDF 索引位置无效')
        elif fmt=='png':
            if data[:8]!=b'\x89PNG\r\n\x1a\n' or data[12:16]!=b'IHDR' or b'IEND' not in data[-16:]:raise ValueError('不是完整 PNG')
            w,h=struct.unpack('>II',data[16:24])
            if not w or not h:raise ValueError('图片尺寸无效')
        elif fmt in ('jpeg','webp','gif'):
            import io
            from PIL import Image
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
        else:
            text=data.decode('utf-8')
            if '\x00' in text:raise ValueError('文本含 NUL')
            if fmt=='json':json.loads(text)
            if fmt=='svg':
                root=ET.fromstring(text)
                if root.tag.split('}')[-1]!='svg':raise ValueError('不是 SVG')
    except ExecutionError:raise
    except Exception as exc:raise ExecutionError('invalid_artifact',str(exc)[:250]) from exc
    return fmt,mime


def output_path(path):
    """相对附件路径以 output 为根；仍沿用工作区的路径边界。"""
    if not isinstance(path, str):raise ExecutionError('path_denied')
    if not path.startswith('/'):
        path = '/workspace/' + path if path.startswith('output/') else '/workspace/output/' + path
    workspace_path(path, write=True)
    if not path.startswith('/workspace/output/'):raise ExecutionError('path_denied')
    return path


def previewable(record):
    return (record['format'] in ('html','pdf','svg','png','jpeg','webp','gif','csv','json','markdown','text','code'))


def prepare_finish(ctx,args,requirements=None):
    """正文文件须可读；附件逐个准备，坏附件不连带退回正文和其他附件。"""
    ctx.pending_artifacts=[]
    ctx.pending_artifact_errors=[]
    draft=args.get('draft','');draft_file=args.get('draft_file');items=args.get('artifacts',[])
    if not draft_file and items==[]:return draft
    if draft_file and 'draft' in args:raise ExecutionError('invalid_arguments','draft 与 draft_file 只能给一个')
    available=ctx.tool_role=='lead' and ctx.config.execution.enabled and ctx.execution is not None
    if draft_file:
        if not available:raise ExecutionError('workspace_unavailable')
        workspace_path(draft_file,write=True,direct=True)
        if not draft_file.startswith('/workspace/work/') or not draft_file.endswith('.md'):raise ExecutionError('path_denied')
        files=ctx.execution.snapshot({draft_file:None})
        try:draft=files[draft_file].decode('utf-8')
        except UnicodeError as exc:raise ExecutionError('invalid_draft','正文须为 UTF-8') from exc
    if not isinstance(items,list):
        ctx.pending_artifact_errors.append({'error_code':'invalid_arguments',
            'message':'附件列表未采用：artifacts 必须是数组'})
        items=[]
    if items and not available:
        ctx.pending_artifact_errors.append({'error_code':'workspace_unavailable','message':'工作区不可用，附件未交付'})
        items=[]
    allowed={r.id for r in (requirements if requirements is not None else ctx.effective_requirements)}
    pending=[];paths=set()
    for item in items:
        path=item.get('path') if isinstance(item,dict) else None
        try:
            request=ArtifactRequest.model_validate(item)
            request.path=output_path(request.path)
            if request.path in paths:raise ExecutionError('duplicate_artifact')
            if not set(request.requirement_ids)<=allowed:raise ExecutionError('unknown_requirement')
            paths.add(request.path)
            filename=PurePosixPath(request.path).name
            data=ctx.execution.snapshot({request.path:None})[request.path]
            fmt,mime=validate_file(filename,data)
            pending.append({**request.model_dump(),'filename':filename,'format':fmt,'mime':mime,'data':data})
        except (ExecutionError,ValueError) as exc:
            if isinstance(exc,ExecutionError) and exc.code in ('cancelled','command_timeout','cancelled_or_deadline'):
                raise
            error={'message':f'附件未交付：{str(exc)[:500]}','error_code':getattr(exc,'code','invalid_arguments')}
            if isinstance(path,str):error['path']=path
            if isinstance(item,dict):error['requirement_ids']=item.get('requirement_ids',[])
            ctx.pending_artifact_errors.append(error)
    if llm.cancellation_requested() or (ctx.budget and ctx.budget.expired()):raise ExecutionError('cancelled_or_deadline')
    ctx.pending_artifacts=pending
    return draft


def missing_formats(requirements,artifacts):
    missing=[]
    for req in requirements:
        items=[a for a in artifacts if req.id in a['requirement_ids']]
        formats={a['format'] for a in items}
        for wanted in req.output_formats:
            normalized={'jpg':'jpeg','md':'markdown','txt':'text'}.get(wanted.lower().lstrip('.'),wanted.lower().lstrip('.'))
            present=normalized in formats
            if wanted=='mindmap':
                maps=[a for a in items if a['kind'] in ('mindmap','mindmap_source')]
                present=any(a['format'] in ('png','svg','pdf') for a in maps) and any(a['format']=='json' for a in maps)
            if not present:missing.append({'requirement_id':req.id,'format':wanted,'message':f'{req.id}：未交付 {wanted} 文件'})
    return missing


def publish(ctx,draft):
    pending=ctx.pending_artifacts
    if not pending:return []
    records=[]; directory=ctx.store.dir/'artifacts'; candidate=None
    try:
        with _PUBLISH_LOCK:
            if directory.exists():raise ExecutionError('artifact_conflict')
            candidate=Path(tempfile.mkdtemp(prefix='.artifact-candidate-',dir=ctx.store.dir))
            for item in pending:
                aid='a_'+uuid4().hex;folder=candidate/aid;folder.mkdir()
                with (folder/item['filename']).open('xb') as handle:
                    handle.write(item['data']);handle.flush();os.fsync(handle.fileno())
                record={k:v for k,v in item.items() if k not in ('data','path')}
                record.update(artifact_id=aid,run_id=ctx.store.run_id,size=len(item['data']),
                    sha256=hashlib.sha256(item['data']).hexdigest(),storage_path=f'artifacts/{aid}/{item["filename"]}',
                    created_at=time.time())
                records.append(record)
            execution_ids=[]
            for receipt in ctx.receipts:
                if receipt.get('tool') not in ('bash','write_file','edit_file'):continue
                try: ident=json.loads(receipt['payload']).get('execution_id')
                except (ValueError,TypeError,KeyError):continue
                if ident:execution_ids.append(ident)
            provenance=dict(draft_sha256=hashlib.sha256(draft.encode()).hexdigest(),
                image_id=ctx.execution.image_id,inputs=dict(ctx.execution.inputs),
                source_materials=ctx.execution_materials,execution_files=list(ctx.execution_files.values()),
                execution_ids=execution_ids,tool_call_id=ctx.active_tool_call_id)
            atomic_json(candidate/'manifest.json',{'artifacts':records,'provenance':provenance})
            if llm.cancellation_requested():raise ExecutionError('cancelled')
            os.replace(candidate,directory);candidate=None
        return records
    except OSError as exc:raise llm.LLMPersistenceError('附件写入失败') from exc
    finally:
        if candidate:shutil.rmtree(candidate)
        ctx.pending_artifacts=[]


def committed_records(payload):
    if not payload or payload.get('draft_rejected') or payload.get('stop_reason') in ('cancelled','failed'):
        return []
    return payload.get('artifacts') or []


def read_committed_file(run_dir, record):
    aid=record.get('artifact_id','');name=record.get('filename','')
    if not re.fullmatch(r'a_[a-f0-9]{32}',aid) or not name or name in ('.','..') or '/' in name or '\x00' in name:
        raise ExecutionError('invalid_artifact')
    root=Path(run_dir).resolve();path=root/'artifacts'/aid/name
    if record.get('storage_path')!=f'artifacts/{aid}/{name}' or path.resolve()!=path:
        raise ExecutionError('invalid_artifact')
    try:
        data=path.read_bytes()
    except OSError as exc:raise ExecutionError('artifact_missing') from exc
    if len(data)!=record.get('size') or hashlib.sha256(data).hexdigest()!=record.get('sha256'):
        raise ExecutionError('artifact_changed','附件校验失败，未提供下载')
    return data
