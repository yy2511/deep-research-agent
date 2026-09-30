"""Docker 执行会话；持久文件按会话隔离，预算和日志按轮记录。"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import select
import socket
import struct
import threading
import time
from uuid import uuid4

import docker
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

MiB = 1024 * 1024
_PENDING: set[str] = set()
_ENV_PATH = Path(__file__).resolve().parents[2] / '.env'


class ExecutionError(ValueError):
    def __init__(self, code: str, message: str = ''):
        self.code = code
        super().__init__(message or code)


class ExecutionConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    enabled: bool = False
    image: str = 'dra-sandbox:local'
    docker_host: str = Field(default='', exclude=True)
    host_workspace_root: str = Field(default='', exclude=True)
    memory_mb: int = Field(default=2048, ge=128, le=8192)
    pids: int = Field(default=128, ge=16, le=256)
    command_timeout_s: float = Field(default=30, gt=0, le=120)
    max_commands: int = Field(default=20, ge=1, le=40)
    idle_timeout_s: float = Field(default=600, ge=0, le=86400)
    idle_check_s: float = Field(default=60, ge=0.1, le=60)
    max_containers: int = Field(default=3, ge=1, le=16)

    @classmethod
    def from_env(cls):
        local = dotenv_values(_ENV_PATH) if _ENV_PATH.is_file() else {}
        # 生命周期早于 provider 初始化；只读取本功能字段，不改写进程的模型/代理变量。
        def value(name, default=''):
            return os.environ.get(name, local.get(name) or default)
        return cls(enabled=value('DRA_EXECUTION_ENABLED').lower() in ('1','true'),
                   image=value('DRA_EXECUTION_IMAGE','dra-sandbox:local'),
                   docker_host=value('DRA_DOCKER_HOST'),
                   host_workspace_root=value('DRA_EXECUTION_HOST_WORKSPACE_ROOT'),
                   idle_timeout_s=value('DRA_EXECUTION_IDLE_TIMEOUT_S','600'),
                   max_containers=value('DRA_EXECUTION_MAX_CONTAINERS','3'))


def workspace_path(value, *, write=False, direct=False):
    if not isinstance(value,str) or '\x00' in value:
        raise ExecutionError('path_denied')
    parts=value.split('/')
    allowed=('work','output') if write else ('input','work','output')
    if len(parts)<3 or parts[:2]!=['','workspace'] or parts[2] not in allowed or any(p in ('','.','..') for p in parts[2:]):
        raise ExecutionError('path_denied')
    if direct and len(parts)!=4: raise ExecutionError('path_denied','交付只接受目录下的直接子文件')
    return value


def decode_snapshot(payload, paths):
    if not isinstance(payload,dict) or not payload.get('ok'):
        raise ExecutionError(payload.get('error_code','export_failed') if isinstance(payload,dict) else 'export_failed')
    files=payload.get('files')
    if not isinstance(files,list) or len(files)!=len(paths): raise ExecutionError('invalid_export')
    result={}
    for item in files:
        path=item.get('path'); encoded=item.get('base64','')
        if path not in paths or path in result or not isinstance(encoded,str) or len(encoded)>4*((paths[path]+2)//3):
            raise ExecutionError('invalid_export')
        try: content=base64.b64decode(encoded,validate=True)
        except ValueError as exc: raise ExecutionError('invalid_export') from exc
        if len(content)>paths[path] or len(content)!=item.get('size') or hashlib.sha256(content).hexdigest()!=item.get('sha256'):
            raise ExecutionError('invalid_export')
        result[path]=content
        if sum(map(len,result.values()))>50*MiB+256*1024: raise ExecutionError('quota_exceeded')
    return result


def docker_client(config):
    address=config.docker_host
    if not address:
        desktop=Path.home()/'.docker/run/docker.sock'
        address='unix://'+str(desktop) if desktop.exists() else 'unix:///var/run/docker.sock'
    if not address.startswith('unix:///'): raise ExecutionError('invalid_configuration','第一版只支持本机 Unix socket')
    return docker.DockerClient(base_url=address,timeout=3)


def availability(config):
    if not config.enabled: return {'enabled':False,'available':False,'reason':'disabled'}
    if _PENDING: return {'enabled':True,'available':False,'reason':'cleanup_pending'}
    client=None
    try:
        client=docker_client(config)
        client.ping()
        image=client.images.get(config.image)
        if image.labels.get('dra.sandbox.protocol')!='2': raise ExecutionError('image_incompatible')
        return {'enabled':True,'available':True,'image_id':image.id}
    except docker.errors.ImageNotFound: reason='image_missing'
    except ExecutionError as exc: reason=exc.code
    except (docker.errors.DockerException,OSError): reason='docker_unavailable'
    finally:
        if client: client.close()
    return {'enabled':True,'available':False,'reason':reason}


def instance_id(run_root):
    return hashlib.sha256(str(Path(run_root).resolve()).encode()).hexdigest()[:20]


def reap_orphans(run_root, config, active_run_ids=()):
    """仅清理由该运行根目录控制、且调用方确认没有活跃线程的容器。"""
    if not config.enabled: return []
    client=None; removed=[]
    marker=f'dra-exec-{instance_id(run_root)}-reaper'
    try:
        client=docker_client(config)
        for container in client.containers.list(all=True,filters={'label':['dra.managed=1',f'dra.instance={instance_id(run_root)}']}):
            if container.labels.get('dra.run') in active_run_ids: continue
            container.remove(force=True)
            _PENDING.discard(container.id); _PENDING.discard(container.name); removed.append(container.id)
        if not active_run_ids:
            prefix=f'dra-exec-{instance_id(run_root)}-'
            _PENDING.difference_update({name for name in _PENDING if name.startswith(prefix)})
    except Exception:
        _PENDING.add(marker)
        raise
    finally:
        if client: client.close()
    return removed


class ExecutionSession:
    def __init__(self, run_dir, run_id, config, *, deadline=None, cancelled=None, conversation_id=None, pool=None):
        from dra.execution_workspace import ConversationWorkspace
        self.run_dir=Path(run_dir); self.run_id=run_id; self.config=config
        self.deadline=deadline if deadline is not None else time.monotonic()+600
        self.cancelled=cancelled or (lambda:False)
        self.directory=self.run_dir/'execution'; self.client=None; self.container=None
        self.lock=threading.RLock(); self.state='new'; self.commands=0; self.log_bytes=0; self.input_bytes=0
        self.inputs={}; self.image_id=None; self.error=None; self.container_name=None
        self.workspace=ConversationWorkspace(run_dir,run_id,config,conversation_id)
        self._last_quota_check=0.0
        self.pool=pool; self.reused=False; self.last_container_id=None

    def _persist(self):
        from dra.durable import atomic_json
        from dra.llm import LLMPersistenceError
        try:
            atomic_json(self.directory/'session.json',self.summary())
        except OSError as exc: raise LLMPersistenceError('执行会话状态无法落盘') from exc

    def summary(self):
        return {'state':self.state,'image_id':self.image_id,'container_id':self.container.id if self.container else self.last_container_id,
                'container_name':self.container_name,'commands':self.commands,'log_bytes':self.log_bytes,'error':self.error,
                'conversation_id':self.workspace.conversation_id,'workspace_persistent':True,'reused_container':self.reused,'limits':self.config.model_dump()}

    def _check(self, deadline=None):
        if self.cancelled(): raise ExecutionError('cancelled')
        if time.monotonic()>=min(self.deadline,deadline if deadline is not None else self.deadline):
            raise ExecutionError('command_timeout')

    def start(self):
        with self.lock:
            if not self.config.enabled: raise ExecutionError('disabled')
            if self.state=='ready': self._check(); return
            if self.state!='new': raise ExecutionError('workspace_unavailable', self.state)
            if _PENDING: raise ExecutionError('cleanup_pending')
            self._check(); self.state='starting'; self._persist()
            try:
                entry=self.pool.acquire(self) if self.pool else None
                if entry:
                    self.client=entry.client; self.container=entry.container
                    self.container_name=entry.name
                else: self.client=docker_client(self.config)
                image=self.client.images.get(self.config.image)
                if image.labels.get('dra.sandbox.protocol')!='2': raise ExecutionError('image_incompatible')
                self.image_id=image.id
                if entry and entry.image_id==image.id:
                    self.reused=True; self.state='ready'
                    self._renew_lease(self.deadline-time.monotonic())
                    receipt=self._exec(['python','-I','/opt/dra-tools/helper.py','--reset-input'],5,user='0:0',logs=False)
                    if not receipt['ok'] or json.loads(receipt['stdout']).get('ok') is not True:
                        raise ExecutionError('input_reset_failed')
                    self._persist()
                    return
                if entry:
                    self.container.remove(force=True)
                    self.container=None; self.container_name=None
                host=self.client.info()
                if host.get('OSType')!='linux' or not all(host.get(k) for k in ('MemoryLimit','CpuCfsQuota','PidsLimit')):
                    raise ExecutionError('limits_unavailable')
                tmpfs={}
                for path,size,uid,mode,inodes in [('input',32,0,'0755',4096)]:
                    tmpfs['/workspace/'+path]=f'rw,noexec,nosuid,nodev,size={size}m,uid={uid},gid={uid},mode={mode},nr_inodes={inodes}'
                tmpfs['/tmp']='rw,noexec,nosuid,nodev,size=128m,mode=1777,nr_inodes=8192'
                tmpfs['/run/dra']='rw,noexec,nosuid,nodev,size=1m,uid=0,gid=0,mode=0755,nr_inodes=16'
                self.workspace.prepare()
                probe=uuid4().hex
                probe_path=self.workspace.path/'work'/('.dra-probe-'+probe)
                probe_path.write_text(probe)
                if os.getuid()==0: os.chown(probe_path,self.workspace.uid,self.workspace.gid)
                self.container_name=f'dra-exec-{instance_id(self.run_dir.parent)}-{uuid4().hex[:12]}'
                self._persist()
                self.container=self.client.containers.create(self.image_id,command=[str(max(1,math.ceil(self.deadline-time.monotonic())))],
                    name=self.container_name,user='65534:65534' if self.workspace.uid!=65534 else '65533:65533',working_dir='/',init=True,
                    read_only=True,network_mode='none',ipc_mode='private',cap_drop=['ALL'],
                    security_opt=['no-new-privileges:true'],tmpfs=tmpfs,mounts=self.workspace.mounts(),shm_size=32*MiB,
                    mem_limit=self.config.memory_mb*MiB,memswap_limit=self.config.memory_mb*MiB,
                    nano_cpus=1_000_000_000,pids_limit=self.config.pids,privileged=False,
                    environment={'HOME':'/tmp','LANG':'C.UTF-8','MPLCONFIGDIR':'/tmp/matplotlib','XDG_CACHE_HOME':'/tmp/cache'},
                    labels={'dra.managed':'1','dra.instance':instance_id(self.run_dir.parent),'dra.run':self.run_id,
                            'dra.conversation':self.workspace.key,
                            'dra.expires':str(time.time()+self.deadline-time.monotonic())},
                    ulimits=[docker.types.Ulimit(name='core',soft=0,hard=0),
                             docker.types.Ulimit(name='fsize',soft=64*MiB,hard=64*MiB)],
                    log_config=docker.types.LogConfig(type='json-file',config={'max-size':'1m','max-file':'1'}),
                    use_config_proxy=False)
                self._persist(); self.container.start(); self.state='ready'
                # 配置已发送还不够：在实际容器中检查 cgroup 与文件权限。
                check="""import json,os,pathlib
p=pathlib.Path('/sys/fs/cgroup')
print(json.dumps({'memory':(p/'memory.max').read_text().strip(),'pids':(p/'pids.max').read_text().strip(),'cpu':(p/'cpu.max').read_text().strip(),'input_writable':os.access('/workspace/input',os.W_OK),'root_readonly':bool(os.statvfs('/').f_flag & os.ST_RDONLY),'uid':os.getuid()}))"""
                check+=f"\nassert pathlib.Path('/workspace/work/.dra-probe-{probe}').read_text()=={probe!r}\n"
                receipt=self._exec(['python','-I','-c',check],5,capture=16384,logs=False)
                probe_path.unlink(missing_ok=True)
                actual=json.loads(receipt['stdout'])
                quota,period=actual['cpu'].split()
                if not (receipt['ok'] and actual['uid']==self.workspace.uid and actual['uid']!=0 and not actual['input_writable'] and actual['root_readonly']
                        and int(actual['memory'])<=self.config.memory_mb*MiB and int(actual['pids'])<=self.config.pids
                        and int(quota)/int(period)<=1): raise ExecutionError('limits_unavailable')
                self._persist()
            except BaseException as exc:
                if 'probe_path' in locals(): probe_path.unlink(missing_ok=True)
                self.error=getattr(exc,'code','docker_unavailable')
                if isinstance(exc,docker.errors.APIError) and exc.response is not None and 400<=exc.response.status_code<500 and self.container is None:
                    self.container_name=None
                self.close()
                if isinstance(exc,(KeyboardInterrupt,SystemExit)): raise
                from dra.llm import LLMPersistenceError
                if isinstance(exc,LLMPersistenceError): raise
                if isinstance(exc,ExecutionError): raise
                raise ExecutionError(self.error) from exc

    def _renew_lease(self, ttl):
        receipt=self._exec(['python','-I','/opt/dra-tools/helper.py','--lease',str(max(1,ttl))],5,user='0:0',logs=False)
        if not receipt['ok'] or json.loads(receipt['stdout']).get('ok') is not True:
            raise ExecutionError('lease_failed')

    def release(self):
        """结束本轮租用。文件保留；只有无后台工作进程的健康容器可待用。"""
        with self.lock:
            if self.state in ('released','closed'): return
            if self.pool is None or self.state!='ready' or self.cancelled() or time.monotonic()>=self.deadline:
                self.close(); return
            try:
                controller='65534:65534' if self.workspace.uid!=65534 else '65533:65533'
                receipt=self._exec(['python','-I','/opt/dra-tools/helper.py','--idle-check',str(self.workspace.uid)],5,user=controller,logs=False)
                data=json.loads(receipt['stdout'])
                if not receipt['ok'] or data.get('ok') is not True or data.get('busy') is not False:
                    self.close(); return
                ttl=(self.pool.config.idle_timeout_s+self.pool.config.idle_check_s+5
                     if self.pool.config.idle_timeout_s else self.pool.config.idle_check_s*2+30)
                self._renew_lease(ttl)
                self.workspace.record()
                if not self.pool.park(self): self.close(); return
                self.last_container_id=self.container.id
                self.container=None; self.client=None; self.state='released'; self._persist()
            except Exception as exc:
                self.error=getattr(exc,'code','release_failed'); self.close()
                from dra.llm import LLMPersistenceError
                if isinstance(exc,LLMPersistenceError): raise

    def _exec(self, argv, timeout, *, capture=8192, logs=True, stdin=None, user=None):
        from dra.llm import LLMPersistenceError
        self._check(); end=min(self.deadline,time.monotonic()+timeout)
        self._last_quota_check=time.monotonic()
        execution_id='exec_'+uuid4().hex; started=time.monotonic()
        token=self.client.api.exec_create(self.container.id,argv,user=user or self.workspace.worker,workdir='/',tty=False,stdin=stdin is not None)['Id']
        stream=None; handles={}; saved=0; outputs={1:bytearray(),2:bytearray()}; total=0
        try:
            if logs:
                try:
                    (self.directory/'logs').mkdir(parents=True,exist_ok=True)
                    for channel,label in [(1,'stdout'),(2,'stderr')]:
                        handles[channel]=(self.directory/'logs'/f'{execution_id}.{label}').open('wb')
                except OSError as exc: raise LLMPersistenceError('执行日志无法创建') from exc
            stream=self.client.api.exec_start(token,socket=True,tty=False)
            raw=getattr(stream,'_sock',stream); raw.setblocking(False)
            buffer=bytearray(); channel=None; remaining=0; sent=0; input_closed=stdin is None
            while True:
                self._check(end)
                if logs and time.monotonic()-self._last_quota_check>=1:
                    self._last_quota_check=time.monotonic()
                    if self.workspace.describe()['over_quota']:
                        raise ExecutionError('workspace_quota','会话目录超出容量限制，已停止执行；文件保留供清理')
                readable,writable,_=select.select([raw],[] if input_closed else [raw],[],0.1)
                if writable:
                    if sent<len(stdin):
                        try: sent+=raw.send(stdin[sent:sent+65536])
                        except BlockingIOError: pass
                    if sent==len(stdin):
                        raw.shutdown(socket.SHUT_WR); input_closed=True
                if not readable: continue
                try: chunk=raw.recv(65536)
                except BlockingIOError: continue
                if not chunk: break
                buffer.extend(chunk)
                while buffer:
                    if channel is None:
                        if len(buffer)<8: break
                        channel,remaining=struct.unpack('>BxxxI',buffer[:8]); del buffer[:8]
                        if channel not in (1,2) or remaining>16*MiB: raise ExecutionError('invalid_stream')
                    take=min(len(buffer),remaining); data=bytes(buffer[:take]); del buffer[:take]; remaining-=take
                    total+=len(data)
                    room=max(0,capture//2-len(outputs[channel])); outputs[channel].extend(data[:room])
                    if logs:
                        keep=data[:max(0,min(MiB-saved,20*MiB-self.log_bytes))]
                        try: handles[channel].write(keep)
                        except OSError as exc: raise LLMPersistenceError('执行日志无法写入') from exc
                        saved+=len(keep); self.log_bytes+=len(keep)
                    if remaining==0: channel=None
                    elif not buffer: break
            while True:
                self._check(end)
                info=self.client.api.exec_inspect(token)
                if not info['Running']: break
                time.sleep(.05)
            code=info['ExitCode']
            if logs and self.workspace.describe()['over_quota']:
                raise ExecutionError('workspace_quota','会话目录超出容量限制，已停止执行；文件保留供清理')
            if code==137:
                raise ExecutionError('process_killed', '进程被强制终止（退出 137，可能达到内存限制）')
            return {'ok':code==0,'error_code':None if code==0 else 'process_exit','exit_code':code,
                    'stdout':outputs[1].decode('utf-8','replace'),'stderr':outputs[2].decode('utf-8','replace'),
                    'truncated':total>sum(map(len,outputs.values())), 'log_truncated':logs and total>saved,
                    'elapsed_ms':round(1000*(time.monotonic()-started)),'execution_id':execution_id,'workspace_available':True}
        except BaseException as exc:
            self.error=getattr(exc,'code','execution_uncertain'); self.close()
            from dra.llm import LLMPersistenceError
            if isinstance(exc,(LLMPersistenceError,KeyboardInterrupt,SystemExit)): raise
            if isinstance(exc,ExecutionError): raise
            raise ExecutionError('execution_uncertain') from exc
        finally:
            if stream:
                response=getattr(stream,'_response',None)
                if response is not None: response.close()
                else: stream.close()
            for handle in handles.values():
                try: handle.close()
                except OSError as exc: raise LLMPersistenceError('执行日志无法保存') from exc

    def tool(self,name,args):
        with self.lock:
            self.start()
            if name not in ('bash','read_file','write_file','edit_file'): raise ExecutionError('invalid_arguments')
            request={**args,'op':name}
            workspace_path(args.get('cwd','/workspace/work') if name=='bash' else args.get('path'),write=name in ('bash','write_file','edit_file'))
            if name=='bash':
                if self.commands>=self.config.max_commands: raise ExecutionError('command_budget')
                command=args.get('command')
                if not isinstance(command,str) or not command or len(command.encode())>32768: raise ExecutionError('invalid_arguments')
                timeout=args.get('timeout_s',self.config.command_timeout_s)
                if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=120: raise ExecutionError('invalid_arguments')
                self.commands+=1; self._persist()
            else: timeout=10
            encoded=base64.b64encode(json.dumps(request,ensure_ascii=False).encode()).decode()
            if len(encoded)>512*1024: raise ExecutionError('quota_exceeded')
            argv=['python','-I','/opt/dra-tools/helper.py',*[encoded[i:i+8192] for i in range(0,len(encoded),8192)]]
            receipt=self._exec(argv,timeout,capture=8192 if name=='bash' else 256*1024)
            self._persist()
            if name=='bash': return receipt
            if not receipt['ok']: return receipt
            try: data=json.loads(receipt['stdout'])
            except ValueError as exc: raise ExecutionError('invalid_file_receipt') from exc
            return {**data,'execution_id':receipt['execution_id'],'workspace_available':True}

    def import_file(self, filename, data):
        with self.lock:
            self.start()
            if not filename or PurePosixPath(filename).name!=filename or '/' in filename or '\x00' in filename or filename in ('.','..'):
                raise ExecutionError('path_denied')
            digest=hashlib.sha256(data).hexdigest()
            if filename in self.inputs:
                if self.inputs[filename]!=digest: raise ExecutionError('file_conflict')
                return '/workspace/input/'+filename
            if self.input_bytes+len(data)>32*MiB: raise ExecutionError('quota_exceeded')
            receipt=self._exec(['python','-I','/opt/dra-tools/helper.py','--import',filename],
                               15,stdin=data,user='0:0',capture=16384,logs=False)
            try: imported=json.loads(receipt['stdout'])
            except ValueError as exc: raise ExecutionError('import_failed') from exc
            if not receipt['ok'] or imported.get('sha256')!=digest:
                raise ExecutionError('import_failed')
            self.input_bytes+=len(data); self.inputs[filename]=digest
            from dra.durable import atomic_json
            from dra.llm import LLMPersistenceError
            try: atomic_json(self.directory/'inputs.json',self.inputs)
            except OSError as exc: raise LLMPersistenceError('导入材料清单无法落盘') from exc
            return '/workspace/input/'+filename

    def snapshot(self, paths: dict[str,int]):
        with self.lock:
            self.start(); self._check()
            if len(paths)>9: raise ExecutionError('quota_exceeded')
            for path,maximum in paths.items():
                workspace_path(path,write=True,direct=True)
                if not 0<maximum<=20*MiB: raise ExecutionError('quota_exceeded')
            encoded=base64.b64encode(json.dumps({'op':'export','paths':paths}).encode()).decode()
            # 文件内容通过受限 exec 流返回；SDK archive 无法访问当前引擎的 tmpfs。
            receipt=self._exec(['python','-I','/opt/dra-tools/helper.py',encoded],10,
                               capture=2*(4*((min(sum(paths.values()),50*MiB+256*1024)+2)//3)+16384),logs=False)
            if not receipt['ok'] or receipt['truncated']: raise ExecutionError('export_failed')
            try: payload=json.loads(receipt['stdout'])
            except ValueError as exc: raise ExecutionError('invalid_export') from exc
            return decode_snapshot(payload,paths)

    def close(self):
        with self.lock:
            if self.state in ('closed','released'): return
            try:
                if self.container:
                    try: self.container.remove(force=True)
                    except docker.errors.NotFound: pass
                    _PENDING.discard(self.container.id)
                if self.container is None and self.container_name:
                    _PENDING.add(self.container_name); self.state='cleanup_pending'
                else:
                    self.state='closed'
            except Exception:
                self.state='cleanup_pending'
                if self.container: _PENDING.add(self.container.id)
            finally:
                if self.pool: self.pool.forget(self)
                if self.client: self.client.close()
                self.workspace.record()
                self._persist()
