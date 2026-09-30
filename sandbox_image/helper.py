"""只在执行容器里调用的文件 helper；CLI 不接受工作区根目录覆盖。"""
import base64
import hashlib
import json
import os
from pathlib import PurePosixPath
import stat
import sys
import time
import uuid


class FileError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def parts(path, write=False):
    if not isinstance(path, str) or '\x00' in path or not path.startswith('/workspace/'):
        raise FileError('path_denied', 'path 必须位于 /workspace')
    result = path.split('/')[2:]
    allowed = ('work', 'output') if write else ('input', 'work', 'output')
    if not result or result[0] not in allowed or any(p in ('', '.', '..') for p in result):
        raise FileError('path_denied', '路径越界或无效')
    return result


def directory(root, segments, create=False):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for segment in segments:
            if create:
                try: os.mkdir(segment, 0o700, dir_fd=fd)
                except FileExistsError: pass
            child = os.open(segment, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        return fd
    except BaseException:
        os.close(fd); raise


def read_regular(fd, name, limit):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        meta = os.fstat(handle)
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
            raise FileError('path_denied', '只支持未链接的普通文件')
        if meta.st_size > limit:
            raise FileError('quota_exceeded', '文件超过本操作的大小上限')
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(handle, min(65536, limit + 1 - len(data)))
            if not chunk: break
            data.extend(chunk)
        if len(data) > limit: raise FileError('quota_exceeded', '文件超过上限')
        return bytes(data)
    finally: os.close(handle)


def operate(request, root='/workspace'):
    op = request['op']
    path = request.get('path', request.get('cwd', '/workspace/work'))
    segments = parts(path, write=op in ('write_file', 'edit_file', 'bash'))
    if op == 'bash':
        fd = directory(root, segments)
        os.fchdir(fd); os.close(fd)
        os.execv('/bin/bash', ['bash', '--noprofile', '--norc', '-c', request['command']])
    if len(segments) < 2: raise FileError('path_denied', '需要文件路径')
    fd = directory(root, segments[:-1], create=op == 'write_file')
    temp = None
    try:
        name = segments[-1]
        if op == 'read_file':
            data = read_regular(fd, name, 2*1024*1024)
            result = {'path':path, 'size':len(data), 'sha256':hashlib.sha256(data).hexdigest()}
            try: text = data.decode('utf-8')
            except UnicodeDecodeError: return {**result, 'binary':True}
            offset = request.get('offset', 0); limit = request.get('limit', 1500)
            if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1800:
                raise FileError('invalid_arguments', 'offset 非负；limit 为 1–1800 字符')
            end = min(offset + limit, len(text))
            return {**result, 'content':text[offset:end], 'total_chars':len(text), 'next_offset':end if end < len(text) else None}
        if op not in ('write_file', 'edit_file'): raise FileError('invalid_arguments', '未知操作')
        try: old = read_regular(fd, name, 256*1024)
        except FileNotFoundError: old = None
        expected = request.get('expected_sha256')
        if old is not None and expected != hashlib.sha256(old).hexdigest():
            raise FileError('file_conflict', '覆盖/编辑需要匹配当前文件 sha256')
        if old is None and (op == 'edit_file' or expected):
            raise FileError('file_conflict', '目标文件不存在')
        if op == 'edit_file':
            before=request.get('old_text'); after=request.get('new_text')
            if not isinstance(before,str) or not before or not isinstance(after,str):
                raise FileError('invalid_arguments', 'old_text 不能为空，new_text 必须是文字')
            text=old.decode('utf-8')
            if text.count(before) != 1: raise FileError('file_conflict', 'old_text 必须恰好命中一次')
            data=text.replace(before,after,1).encode()
        else:
            content=request.get('content')
            if not isinstance(content,str): raise FileError('invalid_arguments','content 必须是文字')
            data=content.encode()
        if len(data) > (256*1024 if op == 'edit_file' else 64*1024):
            raise FileError('quota_exceeded', '写入文件超过大小上限')
        temp='.write-'+uuid.uuid4().hex
        target=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
        with os.fdopen(target,'wb') as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.replace(temp,name,src_dir_fd=fd,dst_dir_fd=fd); temp=None
        return {'path':path,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    finally:
        if temp:
            try: os.unlink(temp,dir_fd=fd)
            except FileNotFoundError: pass
        os.close(fd)


def export_files(paths):
    files=[]; total=0
    for path,maximum in paths.items():
        segments=parts(path,write=True)
        if len(segments)!=2: raise FileError('path_denied','仅导出直接子文件')
        fd=directory('/workspace',segments[:-1])
        try:
            before=os.stat(segments[-1],dir_fd=fd,follow_symlinks=False)
            data=read_regular(fd,segments[-1],maximum)
            second=read_regular(fd,segments[-1],maximum)
            after=os.stat(segments[-1],dir_fd=fd,follow_symlinks=False)
            if (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns) or data!=second:
                raise FileError('file_changed','复制期间文件发生变化，请停止后台写入后重试')
            total+=len(data)
            if total>50*1024*1024+256*1024: raise FileError('quota_exceeded','交付总量超过上限')
            files.append({'path':path,'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),
                          'base64':base64.b64encode(data).decode()})
        finally: os.close(fd)
    return {'files':files}


if __name__ == '__main__':
    try:
        if sys.argv[1]=='--idle-check':
            from pathlib import Path
            uid=int(sys.argv[2]); busy=False
            for path in Path('/proc').glob('[0-9]*/status'):
                try:
                    fields=dict(line.split(':',1) for line in path.read_text().splitlines() if ':' in line)
                    if int(fields['Uid'].split()[0])==uid and not fields['State'].strip().startswith('Z'):
                        busy=True; break
                except (FileNotFoundError,ProcessLookupError): pass
            payload={'ok':True,'busy':busy}
        elif sys.argv[1] in ('--lease','--reset-input'):
            if os.geteuid()!=0: raise FileError('path_denied','仅可信控制端可管理租期和输入')
            if sys.argv[1]=='--lease':
                ttl=float(sys.argv[2])
                if not 0<ttl<=172800: raise FileError('invalid_arguments','租期超出范围')
                fd=directory('/run/dra',[])
                try:
                    handle=os.open('next-lease',os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o444,dir_fd=fd)
                    with os.fdopen(handle,'w') as stream: stream.write(str(time.monotonic()+ttl))
                    os.replace('next-lease','lease',src_dir_fd=fd,dst_dir_fd=fd)
                finally: os.close(fd)
            else:
                fd=directory('/workspace',['input'])
                try:
                    for name in os.listdir(fd): os.unlink(name,dir_fd=fd)
                finally: os.close(fd)
            payload={'ok':True}
        elif sys.argv[1]=='--import':
            name=sys.argv[2]
            if os.geteuid()!=0 or not name or PurePosixPath(name).name!=name or name in ('.','..'):
                raise FileError('path_denied','仅可信控制端可导入')
            data=sys.stdin.buffer.read(32*1024*1024+1)
            if len(data)>32*1024*1024: raise FileError('quota_exceeded','导入过大')
            fd=directory('/workspace',['input'])
            try:
                target=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o444,dir_fd=fd)
                with os.fdopen(target,'wb') as handle: handle.write(data)
            finally: os.close(fd)
            payload={'ok':True,'sha256':hashlib.sha256(data).hexdigest()}
        else:
            req=json.loads(base64.b64decode(''.join(sys.argv[1:]),validate=True))
            payload={'ok':True,**(export_files(req['paths']) if req.get('op')=='export' else operate(req))}
    except (ValueError,KeyError,TypeError,OSError) as exc:
        payload={'ok':False,'error_code':getattr(exc,'code','file_error'),'message':str(exc)}
    print(json.dumps(payload,ensure_ascii=False))
