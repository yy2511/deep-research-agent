"""将真实执行输出接入现有引用链；原始资料与派生结果分别保存。"""
from __future__ import annotations

from copy import deepcopy
from os.path import commonprefix

from dra.llm import LLMPersistenceError
from dra.models import RetrievedDoc


def register_result(ctx, args: dict, payload: dict, declared: list) -> None:
    """只赋予本次 stdout 实际展示部分的引用权限，未展示日志仍须读取。

    declared 是 bash 声明且已核对存在的输入来源；标题写明输入，模型和 Jev 才知道数据从哪来。
    """
    visible = payload['stdout']
    execution_id = payload['execution_id']
    output_truncated = payload.get('stdout_log_truncated', payload.get('log_truncated', False))
    try:
        log = ctx.store.dir / 'execution' / 'logs' / f'{execution_id}.stdout'
        # 日志达到原有保存上限时，回执仍可能比落盘日志长；保留实际展示的输出。
        raw = log.read_bytes().decode('utf-8', 'replace')
        if len(raw) < len(visible):
            raw = visible
        inputs = []
        for item in ctx.execution_materials:
            source = ctx.store.source(item.get('source_ref', ''))
            if source is not None:
                inputs.append({**item, 'run_id': ctx.store.run_id, 'title': source.title,
                               'url': source.url, 'download': source.download,
                               'document': source.document, 'computation': bool(source.computation)})
        provenance = {
            'run_id': ctx.store.run_id, 'execution_id': execution_id,
            'tool_call_id': ctx.active_tool_call_id, 'command': args['command'],
            'cwd': args.get('cwd', '/workspace/work'), 'exit_code': payload['exit_code'],
            'source_mode': ctx.config.source_mode,
            'library_version_ids': ctx.config.library_selection.get('version_ids', []),
            'declared_source_refs': args.get('source_refs', []), 'inputs': inputs,
            'stdout_truncated': output_truncated,
            'stderr': payload.get('stderr', ''),
            'workspace_steps': [
                {'tool': r['tool'], 'arguments': deepcopy(r['args']), 'ok': r['ok']}
                for r in ctx.receipts if r['tool'] in ('write_file', 'edit_file', 'bash')
            ],
            'files': deepcopy(list(ctx.execution_files.values())),
        }
        names = '、'.join(dict.fromkeys(item.title or item.url or item.ref for item in declared))
        source, raw = ctx.store.write_source(RetrievedDoc(
            title=f'计算结果（输入：{names}）', source_url='', snippet='', raw_content=raw),
            computation=provenance)
        if output_truncated and not source.truncated:
            source.truncated = True
            with ctx.store._lock:
                next(s for s in ctx.store.state['sources'] if s['ref'] == source.ref)['truncated'] = True
                ctx.store.commit()
        reading = ctx._local_reading(source.ref)
        # UTF-8 的预览可能截在一个字符中间；仅登记与保存输出确实相同的前缀。
        shown = len(commonprefix([raw, visible]))
        reading.read_range(0, shown)
        with ctx._lock:
            ctx.readings[source.ref] = reading
            ctx.sessions[source.ref] = source.ref
            ctx.read_exposures[source.ref] = [(0, shown)]
        payload.update(source_ref=source.ref, source_kind='computation',
                       shown_range=f'chars=0-{shown}', total_chars=len(raw),
                       has_more=shown < len(raw), next_cursor=shown,
                       note='stdout 已展示部分可直接 save_evidence。来源是本次计算输出，原件、命令及本轮脚本编辑记录可回溯；不要把派生值描述成原文件原话。未展示结果用 read_source 继续读。')
    except Exception as exc:
        raise LLMPersistenceError('计算结果及来源记录保存失败') from exc
