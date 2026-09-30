"""会话专用的持久工作目录；模型不能选择宿主路径或会话身份。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

import docker


class ConversationWorkspace:
    def __init__(self, run_dir, run_id, config, conversation_id=None):
        from dra.execution import ExecutionError
        self.run_dir = Path(run_dir).resolve()
        self.run_id = run_id
        self.conversation_id = conversation_id or run_id
        if conversation_id is not None and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', conversation_id):
            raise ExecutionError('workspace_path_invalid')
        # 无 Web 身份的 Python 调用按真实运行目录隔离；追问使用首轮目录作为身份。
        anchor = self.run_dir.parent/conversation_id if conversation_id else self.run_dir
        # 根目录本身已隔离实例；身份不含机器绝对路径，搬迁数据后仍能找到原文件。
        self.key = hashlib.sha256(anchor.name.encode()).hexdigest()[:32]
        self.root = self.run_dir.parent/'.workspaces'
        self.path = self.root/self.key
        self.uid = os.getuid() or 1000
        self.gid = os.getgid() if os.getuid() else 1000
        self.worker = f'{self.uid}:{self.gid}'
        self.config = config

    def _validate(self, *, create=False):
        from dra.execution import ExecutionError
        # 父目录均由可信控制端管理；容器只见 work/output 这两个叶目录。
        for path in (self.root, self.path, self.path/'work', self.path/'output'):
            if create:
                try: path.mkdir(mode=0o700, parents=False)
                except FileExistsError: pass
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise ExecutionError('workspace_path_invalid')

    def prepare(self):
        self.run_dir.parent.mkdir(parents=True, exist_ok=True)
        self._validate(create=True)
        for name in ('work', 'output'):
            path = self.path/name
            if os.getuid() == 0:
                os.chown(path, self.uid, self.gid)
            os.chmod(path, 0o700)
        return self.path

    def mounts(self):
        from dra.execution import ExecutionError
        self._validate()
        daemon_root = self.config.host_workspace_root
        if daemon_root:
            root = PurePosixPath(daemon_root)
            if not root.is_absolute() or '..' in root.parts:
                raise ExecutionError('workspace_path_invalid', 'Docker 宿主工作目录必须为绝对路径')
            source = root/self.key
        else:
            source = self.path
        return [docker.types.Mount(target=f'/workspace/{name}', source=str(source/name),
                                   type='bind', read_only=False) for name in ('work','output')]

    def describe(self):
        """只列有界路径与大小；不读模型文件内容，不跟随任何链接。"""
        from dra.execution import ExecutionError, MiB
        self._validate()
        files, sizes, count, listing_chars = [], {'work':0,'output':0}, 0, 0
        def scan(fd, relative, bucket, depth=0):
            nonlocal count, listing_chars
            if depth>64:
                raise ExecutionError('workspace_quota', '会话工作目录层级超过限制')
            with os.scandir(fd) as entries:
                for entry in entries:
                    count += 1
                    if count > 20_480:
                        raise ExecutionError('workspace_quota', '会话工作文件数量超过限制')
                    try:
                        meta = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(meta.st_mode):
                            child = os.open(entry.name, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=fd)
                            try: scan(child, relative+'/'+entry.name, bucket, depth+1)
                            finally: os.close(child)
                        elif stat.S_ISREG(meta.st_mode):
                            sizes[bucket] += meta.st_size
                            if len(files) < 30 and listing_chars+len(relative)+len(entry.name)<8192:
                                files.append({'path':relative+'/'+entry.name, 'size':meta.st_size})
                                listing_chars+=len(relative)+len(entry.name)
                    except FileNotFoundError:
                        continue  # 命令执行期间允许原子替换和删除。
        for name in sizes:
            path = self.path/name
            if path.exists():
                fd = os.open(path, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                try: scan(fd, '/workspace/'+name, name)
                finally: os.close(fd)
        metadata = self.path/'workspace.json'
        previous = json.loads(metadata.read_text()) if metadata.exists() else {}
        return {'conversation_id':self.conversation_id, 'persistent':True, 'files':files,
                'listing_truncated':count>len(files), 'bytes':sizes,
                'over_quota':sizes['work']>256*MiB or sizes['output']>64*MiB,
                'previous_run_id':previous.get('last_run_id')}

    def record(self):
        from dra.durable import atomic_json
        from dra.llm import LLMPersistenceError
        if not self.path.exists():
            return
        try:
            atomic_json(self.path/'workspace.json', {'conversation_id':self.conversation_id,
                        'last_run_id':self.run_id})
        except OSError as exc:
            raise LLMPersistenceError('会话工作区身份无法落盘') from exc
