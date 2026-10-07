"""macOS 本机执行：Seatbelt 文件边界与 SRT 网络代理，不裸跑命令。"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from dra.execution import ExecutionError, ExecutionSession, MiB

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT/'sandbox_image'
PYTHON = RUNTIME/'.venv/bin/python'
LAUNCHER = RUNTIME/'native_runner.mjs'
SRT_PACKAGE = ROOT/'web/node_modules/@anthropic-ai/sandbox-runtime/package.json'
SRT_VERSION = '0.0.78'


def native_availability(config):
    result = {'enabled': config.enabled, 'available': False, 'backend': 'native',
              'isolation': 'macOS Seatbelt / SRT', 'session_dependencies': True,
              'resource_limits': False}
    if not config.enabled: return {**result, 'reason': 'disabled'}
    if sys.platform != 'darwin': return {**result, 'reason': 'native_platform_unsupported'}
    if not PYTHON.is_file(): return {**result, 'reason': 'native_python_missing'}
    node=shutil.which('node')
    if not node or not SRT_PACKAGE.is_file(): return {**result, 'reason': 'native_runtime_missing'}
    if json.loads(SRT_PACKAGE.read_text())['version'] != SRT_VERSION:
        return {**result, 'reason': 'native_runtime_incompatible'}
    try:
        check=subprocess.run([node,str(LAUNCHER),'--check'], capture_output=True,text=True,timeout=10)
    except (OSError,subprocess.TimeoutExpired) as exc:
        return {**result,'reason':'native_dependencies_unavailable','detail':str(exc)}
    if check.returncode:
        return {**result,'reason':'native_dependencies_unavailable','detail':check.stdout or check.stderr}
    return {**result, 'available': True, 'runtime_version': SRT_VERSION}


def runtime_info():
    raw=subprocess.check_output([str(PYTHON),'-I','-c',
        'import sys,json; print(json.dumps({"prefix":sys.prefix,"base_prefix":sys.base_prefix,"version":sys.version}))'],
        env={'PATH':'/usr/bin:/bin'}, text=True)
    return json.loads(raw)


def native_policy(workspace, temporary, info, *, network, controller_bin=None):
    # Read-only system runtimes and this conversation, never the user's home or repository.
    # Match only the /etc symlink inode, not its subtree; curl reads this alias.
    read=['/bin','/sbin','/System','/dev','/private/var/select','/et[c]',
          info['prefix'],info['base_prefix'],str(RUNTIME/'helper.py'),str(RUNTIME/'render.py'),
          str(workspace.path),str(temporary)]
    read += ['/usr/'+part for part in ('bin','sbin','lib','libexec','share','include','standalone')]
    read += [prefix+'/'+part for prefix in ('/opt/homebrew','/usr/local')
             for part in ('bin','sbin','opt','Cellar','lib','libexec','share','include','Frameworks')]
    read += ['/Library/'+part for part in ('Apple','Developer','Frameworks','Fonts','Java/JavaVirtualMachines')]
    read += [prefix+'/'+part for prefix in ('/etc','/private/etc')
             for part in ('ssl','localtime','resolv.conf','hosts','services','protocols','passwd','group')]
    if controller_bin: read.append(str(controller_bin))
    return {'allowExternalNetwork':network=='bridge',
        'network':{'allowedDomains':[],'deniedDomains':['*'] if network=='none' else [],
                   'deniedResolvedAddresses':['10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','100.64.0.0/10','fc00::/7'],
                   'allowAllUnixSockets':False,'allowLocalBinding':False},
        'filesystem':{'denyRead':['/'],'allowRead':read,
            'allowWrite':[str(workspace.path/'work'),str(workspace.path/'output'),str(temporary)],
            'denyWrite':['/tmp/claude','/private/tmp/claude',str(workspace.path/'input')]},
        'ripgrep':{'command':shutil.which('rg') or 'rg'}}


# Fixed helper and fixed base Python. The model-controlled overlay is only used by Bash.
HELPER_BOOTSTRAP = '''import base64,json,sys,importlib.util,os
os.closerange(3,os.sysconf('SC_OPEN_MAX'))
spec=importlib.util.spec_from_file_location('dra_helper',sys.argv[1])
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
try:
 req=json.loads(base64.b64decode(sys.stdin.buffer.read(),validate=True))
 result={'ok':True,**h.operate(req,root=sys.argv[2])}
except (ValueError,KeyError,TypeError,OSError) as exc:
 result={'ok':False,'error_code':getattr(exc,'code','file_error'),'message':str(exc)}
print(json.dumps(result,ensure_ascii=False))
'''


class NativeSession(ExecutionSession):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        # The runner's cwd is the workspace, not necessarily the API caller's cwd.
        self.run_dir=self.run_dir.resolve();self.directory=self.run_dir/'execution'
        self.session_dependencies=True
        self.temporary=None; self.process=None; self.info=None; self._request=None;self.cleanup_error=None

    def start(self):
        with self.lock:
            self._check()
            if self.state=='ready': return
            if self.state!='new': raise ExecutionError('workspace_unavailable',self.state)
            state=native_availability(self.config)
            if not state['available']: raise ExecutionError(state['reason'])
            try:
                self.workspace.acquire()
                self.workspace.reset_input()
                self.info=runtime_info()
                # macOS Unix socket paths cannot fit a deeply nested project directory.
                self.temporary=tempfile.TemporaryDirectory(prefix='dra-',dir='/tmp')
                self.temp_path=Path(self.temporary.name).resolve()
                self.policy_path=self.directory/'native-policy.json'
                from dra.durable import atomic_json
                bin_dir=self.directory/'bin';bin_dir.mkdir(parents=True,exist_ok=True)
                renderer=bin_dir/'dra-render'
                renderer.write_text('#!/bin/bash\nexec '+shlex.join([str(PYTHON),'-I',str(RUNTIME/'render.py')])+' "$@"\n')
                renderer.chmod(0o755)
                atomic_json(self.policy_path,native_policy(self.workspace,self.temp_path,self.info,network=self.config.network_mode,controller_bin=bin_dir))
                self.state='ready'; self._persist()
            except BaseException:
                self.close()
                raise

    def _keep_alive(self):
        pass  # 本机命令逐条启动，没有需要续期的常驻容器。

    def summary(self):
        limits={k:getattr(self.config,k) for k in ('command_timeout_s','max_commands','network_mode')}
        return {**super().summary(),'limits':limits,'isolation':'macOS Seatbelt / SRT',
                'runtime_version':SRT_VERSION,'python':self.info,'resource_limits':False,
                'cleanup_error':self.cleanup_error,
                'process_cleanup':'command_process_group', 'session_dependencies':True}

    def _helper_argv(self,encoded):
        # Preserve the existing 512 KiB request contract without macOS ARG_MAX truncation.
        self._request=encoded.encode()
        return [str(PYTHON),'-I','-c',HELPER_BOOTSTRAP,str(RUNTIME/'helper.py'),str(self.workspace.path)]

    def _environment(self):
        root=self.workspace.path
        key=hashlib.sha256((RUNTIME/'uv.lock').read_bytes()+self.info['version'].encode()).hexdigest()[:12]
        return {'PATH':f'{self.directory}/bin:{PYTHON.parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
            'HOME':str(self.temp_path),'LANG':'en_US.UTF-8','PYTHONDONTWRITEBYTECODE':'1',
            'PYTHONUNBUFFERED':'1','TMPDIR':str(self.temp_path),'CLAUDE_CODE_TMPDIR':str(self.temp_path),
            'DRA_WORKSPACE':str(root),'DRA_INPUT':str(root/'input'),'DRA_WORK':str(root/'work'),
            'DRA_OUTPUT':str(root/'output'),'DRA_DEPENDENCY_KEY':'native-'+key}

    def _exec(self,argv,timeout,*,capture=8192,logs=True,stdin=None,user=None,managed=None):
        from dra.llm import LLMPersistenceError
        self._check(); started=time.monotonic(); end=min(self.deadline,started+timeout)
        execution_id='exec_'+uuid4().hex
        request=self._request;self._request=None;sent=0
        outputs={1:bytearray(),2:bytearray()}; totals={1:0,2:0}; saved=0; stdout_saved=0
        handles={}; selector=selectors.DefaultSelector(); process=None
        try:
            if logs:
                try:
                    (self.directory/'logs').mkdir(parents=True,exist_ok=True)
                    for channel,label in ((1,'stdout'),(2,'stderr')):
                        handles[channel]=(self.directory/'logs'/f'{execution_id}.{label}').open('wb')
                except OSError as exc:raise LLMPersistenceError('执行日志无法创建') from exc
            expires_ms=math.ceil((time.time()+end-time.monotonic())*1000)
            process=subprocess.Popen([shutil.which('node'),str(LAUNCHER),str(self.policy_path),str(expires_ms),str(os.getpid()),*argv],
                cwd=self.workspace.path/'work',env=self._environment(),start_new_session=True,
                # Only the trusted runner inherits the lease; it does not pass it to Bash.
                pass_fds=(self.workspace._lease.fileno(),),
                stdin=subprocess.PIPE if request is not None else subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            self.process=process
            if request is not None:
                os.set_blocking(process.stdin.fileno(),False)
                selector.register(process.stdin,selectors.EVENT_WRITE,0)
            for channel,pipe in ((1,process.stdout),(2,process.stderr)):
                os.set_blocking(pipe.fileno(),False);selector.register(pipe,selectors.EVENT_READ,channel)
            while selector.get_map():
                self._check(end)
                for key,_ in selector.select(.1):
                    if key.data==0:
                        try:sent+=os.write(key.fd,request[sent:sent+65536])
                        except BrokenPipeError:sent=len(request)
                        if sent==len(request):
                            selector.unregister(key.fileobj);key.fileobj.close()
                        continue
                    data=os.read(key.fd,65536); channel=key.data
                    if not data:
                        selector.unregister(key.fileobj);continue
                    totals[channel]+=len(data)
                    outputs[channel].extend(data[:max(0,capture//2-len(outputs[channel]))])
                    if logs:
                        keep=data[:max(0,min(MiB-saved,20*MiB-self.log_bytes))]
                        try:handles[channel].write(keep)
                        except OSError as exc:raise LLMPersistenceError('执行日志无法写入') from exc
                        saved+=len(keep);self.log_bytes+=len(keep)
                        if channel==1:stdout_saved+=len(keep)
            code=process.wait(timeout=max(.01,end-time.monotonic()))
            self._check(end)
            return {'ok':code==0,'error_code':None if code==0 else 'process_exit','exit_code':code,
                'stdout':outputs[1].decode('utf-8','replace'),'stderr':outputs[2].decode('utf-8','replace'),
                'truncated':sum(totals.values())>sum(map(len,outputs.values())),
                'log_truncated':logs and sum(totals.values())>saved,
                'stdout_log_truncated':logs and totals[1]>stdout_saved,
                'elapsed_ms':round((time.monotonic()-started)*1000),'execution_id':execution_id,
                'workspace_available':True,'backend':'native'}
        except subprocess.TimeoutExpired as exc:
            self.error='command_timeout';raise ExecutionError('command_timeout') from exc
        except ExecutionError as exc:
            self.error=exc.code;raise
        except OSError as exc:
            raise ExecutionError('native_execution_failed',str(exc)) from exc
        finally:
            if process:
                # Never signal another conversation or the service's process group.
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait()
                if process.stdin:process.stdin.close()
                process.stdout.close();process.stderr.close()
            self.process=None;selector.close()
            for handle in handles.values():
                try:handle.close()
                except OSError as exc:raise LLMPersistenceError('执行日志无法保存') from exc

    def release(self):
        self.close()

    def close(self):
        with self.lock:
            if self.state=='closed': return
            try:
                if self.temporary:
                    try:self.temporary.cleanup();self.temporary=None
                    except OSError as exc:
                        self.cleanup_error=str(exc);self.error='native_cleanup_failed'
                if self.workspace._lease is not None:self.workspace.record()
            finally:
                self.workspace.release()
                self.state='closed';self._persist()
