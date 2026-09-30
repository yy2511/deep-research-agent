"""Research method packages: immutable catalogs and per-Agent activation."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import threading

import yaml

from dra.durable import atomic_json

MAX_FILE = 40_000
MAX_PACKAGE = 160_000
MAX_CATALOG = 640_000
MAX_ACTIVE = 60_000
_LOCK = threading.RLock()
NAME = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*')
INSTRUCTIONS = '''\n【研究技能】技能是研究方法，不是事实来源，不能引用为证据或放宽取证、来源范围、预算及工具权限。
用户当前明确要求优先于方法中的默认建议。按相关性调用 load_skill(name)，不要只凭简介猜测正文。
参考材料仅提供方法；需要外部事实仍须检索、阅读和存证。包内脚本不提供执行支持。
'''


class SkillError(ValueError):
    pass


class _Loader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise SkillError('YAML 字段必须是唯一的字符串键')
        result[key] = loader.construct_object(value_node)
    return result


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _name(name):
    if not isinstance(name, str) or len(name) > 64 or not NAME.fullmatch(name):
        raise SkillError('技能标识须为小写字母、数字与单个连字符，最多 64 字符')
    return name


def _reference(path):
    if not isinstance(path, str):
        raise SkillError('参考文件路径必须是字符串')
    p = PurePosixPath(path)
    if not path.startswith('references/') or '\\' in path or '..' in p.parts or str(p) != path or p.suffix not in {'.md', '.txt', '.csv', '.json'}:
        raise SkillError('参考文件仅支持 references/ 内的 md、txt、csv、json 文本')
    return path


def _hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def parse_skill(name, text, references):
    _name(name)
    if not isinstance(text, str) or len(text.encode()) > MAX_FILE:
        raise SkillError('SKILL.md 超过 40 KB 或不是文本')
    parts = re.split(r'^---\s*$', text, maxsplit=2, flags=re.MULTILINE)
    if len(parts) != 3 or parts[0].strip():
        raise SkillError('缺少 YAML frontmatter 围栏')
    try:
        front = yaml.load(parts[1], Loader=_Loader)
    except (yaml.YAMLError, RecursionError, TypeError) as exc:
        raise SkillError('无法解析 YAML frontmatter') from exc
    if not isinstance(front, dict) or front.get('name') != name:
        raise SkillError('name 必须与目录标识一致')
    description = front.get('description')
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1024:
        raise SkillError('description 必须是 1–1024 字符的非空文本')
    if not parts[2].strip():
        raise SkillError('技能正文不能为空')
    if not isinstance(references, dict) or len(references) > 24:
        raise SkillError('每个技能最多 24 份参考文件')
    for path, content in references.items():
        _reference(path)
        if not isinstance(content, str) or len(content.encode()) > MAX_FILE:
            raise SkillError('参考文件超过 40 KB 或不是文本')
    if len(text.encode()) + sum(len(c.encode()) for c in references.values()) > MAX_PACKAGE:
        raise SkillError('技能包超过 160 KB')
    metadata = front.get('metadata')
    title = metadata.get('display-name', name) if isinstance(metadata, dict) else name
    if not isinstance(title, str) or len(title) > 100:
        raise SkillError('display-name 必须为最多 100 字符的文本')
    return {'name': name, 'title': title, 'description': description.strip(), 'body': parts[2].strip(),
            'text': text, 'references': dict(sorted(references.items())),
            'hash': _hash({'text': text, 'references': references})}


class SkillRepository:
    def __init__(self, data_dir, builtin_dir=None):
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / 'skills'
        self.builtin = Path(builtin_dir) if builtin_dir is not None else Path(__file__).with_name('builtin_skills')
        self.settings = self.data_dir / 'skills.json'
        self.snapshots = self.data_dir / 'skill_catalogs'

    def _read(self, root, name):
        path = root / name
        if path.is_symlink() or not path.is_dir():
            raise SkillError('技能目录不存在或是符号链接')
        def read(file):
            if file.is_symlink() or any(p.is_symlink() for p in file.parents if p.is_relative_to(root)):
                raise SkillError('技能包不接受符号链接')
            if file.stat().st_size > MAX_FILE:
                raise SkillError('单个技能文件超过 40 KB')
            return file.read_text(encoding='utf-8')
        refs = {}
        reference_root = path / 'references'
        if reference_root.is_symlink():
            raise SkillError('参考目录不接受符号链接')
        if reference_root.exists():
            for file in reference_root.rglob('*'):
                if file.is_symlink():
                    raise SkillError('参考文件不接受符号链接')
                if file.is_file():
                    relative = file.relative_to(path).as_posix()
                    _reference(relative)
                    refs[relative] = read(file)
                    if len(refs) > 24:
                        raise SkillError('每个技能最多 24 份参考文件')
        text = read(path / 'SKILL.md')
        try:
            return parse_skill(name, text, refs)
        except SkillError as exc:
            exc.draft = {'text': text, 'references': refs}
            raise

    def list(self):
        with _LOCK:
            enabled = None
            if self.settings.exists():
                try:
                    data = json.loads(self.settings.read_text())
                    if data.get('schema_version') != 1 or not isinstance(data.get('enabled'), list) or len(data['enabled']) > 32:
                        raise ValueError()
                    enabled = {_name(n) for n in data['enabled']}
                except (ValueError, OSError, TypeError) as exc:
                    raise SkillError('技能设置损坏；原文件已保留') from exc
            candidates = {}
            for root in (self.builtin, self.root):
                if root.exists():
                    for path in sorted(root.iterdir()):
                        if not path.name.startswith('.'):
                            candidates[path.name] = root
            if len(candidates) > 128:
                raise SkillError('技能目录最多支持 128 项')
            defaults = {p.name for p in self.builtin.iterdir() if p.is_dir()} if self.builtin.exists() else set()
            entries = []
            for name, root in candidates.items():
                entry = {'name': name, 'builtin': root == self.builtin, 'enabled': name in enabled if enabled is not None else name in defaults}
                try:
                    entry.update(self._read(root, name))
                except (SkillError, OSError, UnicodeError) as exc:
                    entry.update(error=str(exc), enabled=False, **getattr(exc, 'draft', {}))
                entries.append(entry)
            return {'skills': entries}

    def set_enabled(self, enabled):
        with _LOCK:
            available = {s['name'] for s in self.list()['skills'] if not s.get('error')}
            if len(enabled) > 32 or len(set(enabled)) != len(enabled) or not set(enabled) <= available:
                raise SkillError('启用集含无效/重复技能，或超过 32 项')
            atomic_json(self.settings, {'schema_version': 1, 'enabled': enabled})

    def save(self, name, text, references):
        parsed = parse_skill(name, text, references)
        with _LOCK:
            self.root.mkdir(parents=True, exist_ok=True)
            target = self.root / name
            if target.is_symlink():
                raise SkillError('技能目录不接受符号链接')
            staging = Path(tempfile.mkdtemp(prefix='.skill-', dir=self.root))
            backup = staging / 'previous'
            package = staging / 'package'
            package.mkdir()
            try:
                # Preserve assets/scripts outside the editor's supported text surface.
                if target.exists():
                    shutil.copytree(target, package, dirs_exist_ok=True, symlinks=True)
                skill_file = package / 'SKILL.md'
                if skill_file.is_symlink():
                    skill_file.unlink()
                reference_dir = package / 'references'
                if reference_dir.is_symlink():
                    reference_dir.unlink()
                elif reference_dir.exists():
                    shutil.rmtree(reference_dir)
                skill_file.write_text(text, encoding='utf-8')
                for path, content in references.items():
                    file = package / path
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_text(content, encoding='utf-8')
                if target.exists():
                    target.rename(backup)
                try:
                    package.rename(target)
                except OSError:
                    if backup.exists():
                        backup.rename(target)
                    raise
                if backup.exists():
                    shutil.rmtree(backup)
            finally:
                if not backup.exists():
                    shutil.rmtree(staging)
            return parsed

    def prepare(self, selected):
        with _LOCK:
            catalog = {s['name']: {k: v for k, v in s.items() if k not in {'builtin', 'enabled'}}
                       for s in self.list()['skills'] if s['enabled'] and not s.get('error')}
            if not set(selected) <= catalog.keys() or len(set(selected)) != len(selected):
                raise SkillError('手选技能不存在、未启用或重复')
            payload = {'skills': catalog, 'selected': sorted(selected)}
            if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_CATALOG:
                raise SkillError('启用技能包总量超过 640 KB，请减少启用项')
            if sum(len(catalog[n]['body']) for n in selected) > MAX_ACTIVE:
                raise SkillError('手选技能正文总量超过 60000 字符')
            bundle = {'id': _hash(payload), **payload}
            path = self.snapshots / (bundle['id'] + '.json')
            if not path.exists():
                atomic_json(path, bundle)
            return bundle

    def resolve(self, identity):
        if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{64}', identity):
            raise SkillError('技能选择标识无效，请重新选择')
        try:
            data = json.loads((self.snapshots / (identity + '.json')).read_text())
        except (OSError, ValueError) as exc:
            raise SkillError('技能快照不存在或损坏，请重新选择') from exc
        if _hash({k: v for k, v in data.items() if k != 'id'}) != identity:
            raise SkillError('技能快照校验失败')
        return data


def disclosure(bundle):
    if not bundle.get('skills'):
        return ''
    return INSTRUCTIONS + json.dumps([{k: s[k] for k in ('name', 'title', 'description')}
                                       for s in bundle['skills'].values()], ensure_ascii=False)


def selected_prompt(bundle):
    if not bundle.get('selected'):
        return ''
    return INSTRUCTIONS + '\n用户为本轮选择的研究方法：\n' + '\n'.join(
        f"【{name}】\n{bundle['skills'][name]['body']}" for name in bundle['selected'])


def schemas(bundle):
    names = list(bundle.get('skills', {}))
    if not names:
        return []
    return [{'type': 'function', 'function': {'name': tool, 'description': description,
            'parameters': {'type': 'object', 'properties': {'name': {'type': 'string', 'enum': names}, **extra},
                           'required': ['name', *extra], 'additionalProperties': False}}}
            for tool, description, extra in [
                ('load_skill', '加载研究方法。先加载正文，再按需读取列出的参考文件。不是证据来源。', {}),
                ('read_skill_reference', '读取已加载技能的参考文本。不是证据来源。', {'path': {'type': 'string'}})]]


class SkillSession:
    def __init__(self, bundle):
        self.bundle = deepcopy(bundle or {'skills': {}, 'selected': []})
        self.loaded = {}

    def prompt(self):
        if not self.loaded:
            return ''
        return INSTRUCTIONS + '\n【已加载研究方法与参考材料】\n' + json.dumps(self.loaded, ensure_ascii=False)

    def execute(self, ctx, tool, args, source='automatic'):
        name = args.get('name')
        skill = self.bundle.get('skills', {}).get(name) if isinstance(name, str) else None
        if not skill:
            return json.dumps({'ok': False, 'error': '技能不存在或未启用'}, ensure_ascii=False), False
        path = args.get('path') if tool == 'read_skill_reference' else None
        if tool == 'read_skill_reference' and (name not in self.loaded or not isinstance(path, str) or path not in skill['references']):
            return json.dumps({'ok': False, 'error': '请先加载技能，并使用列出的参考路径'}, ensure_ascii=False), False
        key = f'{name}/{path}' if path else name
        content = skill['references'][path] if path else skill['body']
        changed = key not in self.loaded
        if changed and len(self.prompt()) + len(content) > MAX_ACTIVE:
            return json.dumps({'ok': False, 'error': '已加载方法材料超过 60000 字符'}, ensure_ascii=False), False
        receipt = {'ok': True, 'changed': changed, 'name': name, 'title': skill['title'], 'hash': skill['hash'],
                   'path': path, 'references': list(skill['references'])}
        if changed:
            from dra import events
            entry = {**receipt, 'content': content, 'source': source, 'agent_id': ctx.delegation_id or ctx.tool_role}
            with ctx.store._lock:
                record = ctx.store.state.setdefault('skills', {'catalog_id': self.bundle.get('id'), 'loaded': []})
                record['loaded'].append(entry)
                ctx._commit_ledger()
            self.loaded[key] = {k: entry[k] for k in ('name', 'path', 'content', 'hash')}
            events.emit('skill_loaded', **{k: v for k, v in entry.items() if k != 'content'})
            receipt['content'] = content
        else:
            receipt['note'] = '方法已在当前上下文中，不重复注入。'
        return json.dumps(receipt, ensure_ascii=False), True
