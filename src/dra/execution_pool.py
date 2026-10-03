"""单控制进程的会话容器池；活跃租用与闲置容器分别管理。"""
from __future__ import annotations

import atexit
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import threading
import time

import docker

from dra import execution
from dra.execution import ExecutionError

logger = logging.getLogger(__name__)


@dataclass
class WarmContainer:
    client: object
    container: object
    image_id: str
    name: str
    policy: tuple
    released_at: float


def _policy(session):
    config=session.config
    return (config.image,config.memory_mb,config.pids,config.docker_host,config.network_mode,
            config.host_workspace_root,session.workspace.worker)


class ExecutionPool:
    def __init__(self, run_root, config, *, start_checker=True):
        self.root=Path(run_root).resolve(); self.config=config
        self.lock=threading.RLock(); self.active={}; self.warm={}
        self.closed=False; self.stop=threading.Event(); self.thread=None
        if start_checker:
            self.thread=threading.Thread(target=self._loop,daemon=True,name='dra-execution-idle')
            self.thread.start()

    def _destroy(self, key):
        entry=self.warm[key]
        try:
            try: entry.container.remove(force=True)
            except docker.errors.NotFound: pass
        except Exception as exc:
            execution._PENDING.add(entry.container.id)
            raise ExecutionError('cleanup_pending') from exc
        execution._PENDING.discard(entry.container.id)
        self.warm.pop(key)
        entry.client.close()

    def acquire(self, session):
        key=session.workspace.key
        with self.lock:
            if self.closed: raise ExecutionError('pool_closed')
            if key in self.active: raise ExecutionError('workspace_busy')
            self.reap()
            entry=self.warm.get(key)
            if entry:
                try:
                    entry.container.reload()
                    healthy=entry.container.attrs['State']['Running'] and entry.policy==_policy(session)
                except docker.errors.DockerException:
                    healthy=False
                if not healthy:
                    self._destroy(key); entry=None
            if entry:
                self.warm.pop(key)
            elif len(self.active)+len(self.warm)>=self.config.max_containers and self.warm:
                oldest=min(self.warm,key=lambda k:self.warm[k].released_at)
                self._destroy(oldest)
            # 容量是软目标；只淘汰闲置容器，不杀正在运行的研究。
            self.active[key]=session
            return entry

    def park(self, session):
        key=session.workspace.key
        with self.lock:
            if self.closed or self.active.get(key) is not session: return False
            self.active.pop(key)
            self.warm[key]=WarmContainer(session.client,session.container,session.image_id,
                session.container_name,_policy(session),time.monotonic())
            return True

    def forget(self, session):
        with self.lock:
            key=session.workspace.key
            if self.active.get(key) is session: self.active.pop(key)

    def reap(self, *, now=None):
        with self.lock:
            now=time.monotonic() if now is None else now
            for key,entry in list(self.warm.items()):
                if self.config.idle_timeout_s and now-entry.released_at>=self.config.idle_timeout_s:
                    self._destroy(key)
                elif not self.config.idle_timeout_s:
                    # 禁用空闲回收也保留控制器失联保险：必须定期续租。
                    self._renew(entry, self.config.idle_check_s*2+30)

    @staticmethod
    def _renew(entry, ttl):
        receipt=entry.container.exec_run(['python','-I','/opt/dra-tools/helper.py','--lease',str(ttl)],user='0:0')
        if receipt.exit_code!=0 or json.loads(receipt.output).get('ok') is not True:
            raise ExecutionError('lease_failed')

    def _loop(self):
        while not self.stop.wait(self.config.idle_check_s):
            try: self.reap()
            except Exception: logger.exception('会话闲置容器回收失败，稍后重试')

    def shutdown(self):
        self.stop.set()
        with self.lock:
            if self.closed: return
            self.closed=True
            sessions=list(self.active.values())
        for session in sessions:
            try: session.close()
            except Exception: logger.exception('关闭活跃执行容器失败')
        with self.lock:
            for key in list(self.warm):
                try: self._destroy(key)
                except Exception: logger.exception('关闭闲置执行容器失败')
        if self.thread and self.thread is not threading.current_thread(): self.thread.join(timeout=5)


_POOLS={}
_LOCK=threading.Lock()


def get_pool(run_root, config):
    key=(str(Path(run_root).resolve()),config.docker_host)
    with _LOCK:
        pool=_POOLS.get(key)
        if pool is None or pool.closed:
            pool=ExecutionPool(run_root,config); _POOLS[key]=pool
        return pool


def shutdown_pools(run_root=None):
    with _LOCK:
        selected=[(key,pool) for key,pool in _POOLS.items()
                  if run_root is None or key[0]==str(Path(run_root).resolve())]
        for key,_ in selected: _POOLS.pop(key)
    for _,pool in selected: pool.shutdown()


atexit.register(shutdown_pools)
