"""历史运行接口：列表、详情、事件回放、来源原文、产物、执行日志。"""
from __future__ import annotations

import hashlib
import html
import json
import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from dra.api.common import _compact_event, _original_response, _require_valid_run_id
from dra.api.deps import RegistryDep, StoreDep
from dra.api.services.runs import _run_input_snapshot
from dra.runstore import IncompatibleEventSchemaError, RunStore, replay_compatibility

router = APIRouter()


@router.get("/api/runs")
def api_runs(*, store: StoreDep) -> list[dict]:
    return store.list_runs()


@router.get("/api/runs/{run_id}/mcp-results/{receipt_id}")
def api_mcp_receipt(run_id: str, receipt_id: str, *, store: StoreDep):
    _require_valid_run_id(run_id)
    if not re.fullmatch(r"[a-f0-9]{32}", receipt_id):
        raise HTTPException(404, "MCP 回执不存在")
    path = store._dir(run_id) / "mcp_results" / f"{receipt_id}.json"
    if not path.is_file() or path.resolve().parent != path.parent.resolve():
        raise HTTPException(404, "MCP 回执不存在")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise HTTPException(503, "MCP 回执读取失败") from None


@router.get("/api/runs/{run_id}/images/{sha256}")
def api_run_image(run_id: str, sha256: str, *, store: StoreDep):
    _require_valid_run_id(run_id)
    if not re.fullmatch(r"[a-f0-9]{64}", sha256) or store.get_meta(run_id) is None:
        raise HTTPException(404, "图片不存在")
    try:
        data = (store._dir(run_id) / "images" / (sha256 + ".png")).read_bytes()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ValueError("hash mismatch")
        return Response(data, media_type="image/png", headers={"X-Content-Type-Options": "nosniff"})
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "图片缺失或校验失败") from exc


@router.get("/api/runs/{run_id}/sources/{ref}/original")
def api_run_original(run_id: str, ref: str, *, store: StoreDep):
    _require_valid_run_id(run_id)
    if not re.fullmatch(r"s[0-9]{1,6}", ref) or store.get_meta(run_id) is None:
        raise HTTPException(404, "来源不存在")
    try:
        directory = store.root / run_id
        ledger = json.loads((directory / "run.json").read_text())
        source = next(s for s in ledger["sources"] if s["ref"] == ref and s["snapshot_kind"] in ("full", "original"))
        document = source.get("document") or source.get("download")
        path = directory / "sources" / f"{ref}.original"
        if path.resolve().parent != (directory / "sources").resolve():
            raise ValueError("非法来源路径")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != document["original_sha256"]:
            raise ValueError("hash mismatch")
        if source.get("download"):
            return Response(data, media_type="application/octet-stream", headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(document["filename"]),
                "X-Content-Type-Options": "nosniff"})
        return _original_response(data, document)
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        raise HTTPException(404, "该运行未保存资料原件或原件校验失败") from exc


@router.get("/api/runs/{run_id}")
def api_run_detail(run_id: str, *, store: StoreDep, registry: RegistryDep) -> dict:
    _require_valid_run_id(run_id)
    meta = store.get_meta(run_id)
    if meta is None:
        raise HTTPException(404, "run 不存在")
    compatible, error = replay_compatibility(meta)
    return {
        "research_input": _run_input_snapshot(run_id, store, registry),
        "meta": {
            **meta,
            "replay_compatible": compatible,
            "replay_error": error,
        },
        "report_md": store.read_report(run_id),
        # simple 运行的真实执行口径（与进度事件一致）；plan 运行不会执行过工具账本
        "executed_tool_calls": store.executed_tool_calls(run_id),
    }


@router.get("/api/runs/{run_id}/artifacts")
def api_artifact_list(run_id: str, *, store: StoreDep):
    from dra.artifacts import committed_records
    _require_valid_run_id(run_id)
    if store.get_meta(run_id) is None: raise HTTPException(404, "run not found")
    return {"artifacts": committed_records(store.read_simple_result(run_id))}


def _artifact_response(store: RunStore, run_id, artifact_id, preview=False):
    from dra.artifacts import committed_records, read_committed_file, previewable, ExecutionError
    from fastapi.responses import Response
    from urllib.parse import quote
    _require_valid_run_id(run_id)
    if store.get_meta(run_id) is None: raise HTTPException(404, "run not found")
    item=next((a for a in committed_records(store.read_simple_result(run_id)) if a.get('artifact_id')==artifact_id),None)
    if item is None: raise HTTPException(404, "artifact not found")
    try: data=read_committed_file(store._dir(run_id),item)
    except ExecutionError as exc: raise HTTPException(409,str(exc)) from exc
    # 个人 demo：按文件本身的浏览器能力预览，不额外封锁脚本和外部资源。
    headers={"X-Content-Type-Options":"nosniff", "Cache-Control":"private, no-store"}
    if preview and not previewable(item): raise HTTPException(415,"此格式请下载后打开")
    disposition='inline' if preview else 'attachment'
    headers['Content-Disposition']=disposition+"; filename*=UTF-8''"+quote(item['filename'],safe='')
    media=item['mime'] if not preview or item['format'] in ('html','pdf','svg','png','jpeg','webp','gif') else 'text/plain'
    return Response(data,media_type=media,headers=headers)


@router.get("/api/runs/{run_id}/artifacts/{artifact_id}/download")
def api_artifact_download(run_id: str,artifact_id: str, *, store: StoreDep):
    return _artifact_response(store,run_id,artifact_id)


@router.get("/api/runs/{run_id}/artifacts/{artifact_id}/preview")
def api_artifact_preview(run_id: str,artifact_id: str, *, store: StoreDep):
    return _artifact_response(store,run_id,artifact_id,preview=True)


@router.get("/api/runs/{run_id}/execution/logs/{execution_id}")
def api_execution_log(run_id: str,execution_id: str,stream: str='stdout',cursor: int=0, *, store: StoreDep):
    _require_valid_run_id(run_id)
    if store.get_meta(run_id) is None: raise HTTPException(404,"run not found")
    if not re.fullmatch(r'exec_[a-f0-9]{32}',execution_id) or stream not in ('stdout','stderr') or cursor<0:
        raise HTTPException(400,"invalid log request")
    root=store._dir(run_id).resolve();path=root/'execution'/'logs'/f'{execution_id}.{stream}'
    if not path.is_file() or path.resolve()!=path: raise HTTPException(404,"log not found")
    size=path.stat().st_size
    if cursor>size:raise HTTPException(400,"invalid cursor")
    with path.open('rb') as handle:
        handle.seek(cursor);data=handle.read(65536)
    return {"text":data.decode('utf-8','replace'),"next_cursor":cursor+len(data) if cursor+len(data)<size else None,"size":size}


@router.get("/api/runs/{run_id}/simple")
def api_run_simple(run_id: str, *, store: StoreDep) -> dict:
    """simple 运行的四状态结果（刷新/历史回看与结束时一致）。

    非 simple 运行没有这份结果：返回 404，前端据此回退到事件回放，不伪造。
    """
    _require_valid_run_id(run_id)
    if store.get_meta(run_id) is None:
        raise HTTPException(404, "run 不存在")
    payload = store.read_simple_result(run_id)
    if payload is None:
        raise HTTPException(404, "该运行没有 simple 结果")
    return payload


@router.get("/api/runs/{run_id}/context/{checkpoint_id}")
def api_context_history(run_id: str, checkpoint_id: str, cursor: int = 0, *, store: StoreDep) -> dict:
    """只读分页；ID 限定当前 run 内已落盘整理记录，不接受文件路径。"""
    _require_valid_run_id(run_id)
    if store.get_meta(run_id) is None:
        raise HTTPException(404, "run 不存在")
    if not re.fullmatch(r"ctx-[0-9a-f]{32}", checkpoint_id) or cursor < 0:
        raise HTTPException(400, "整理记录 ID 或 cursor 无效")
    paths = list((store._dir(run_id) / "context").glob(f"*/{checkpoint_id}.json"))
    if len(paths) != 1:
        raise HTTPException(404, "整理记录不存在")
    try:
        data = json.loads(paths[0].read_text(encoding="utf-8"))
        raw = json.dumps(data.get("raw_exchanges", data["messages"]), ensure_ascii=False)
    except (OSError, ValueError, KeyError):
        raise HTTPException(503, "整理记录暂时无法读取")
    if cursor > len(raw):
        raise HTTPException(400, "cursor 超出记录范围")
    end = min(cursor + 8000, len(raw))
    return {"text": raw[cursor:end], "cursor": cursor, "fragment": True,
            "next_cursor": end if end < len(raw) else None}


@router.get("/api/runs/{run_id}/materials")
def api_run_materials(run_id: str, *, store: StoreDep) -> dict:
    """运行期间也可读已持久化卡片；不把未交付草稿当报告。"""
    _require_valid_run_id(run_id)
    if store.get_meta(run_id) is None:
        raise HTTPException(404, "run 不存在")
    path = store._dir(run_id) / "run.json"
    if not path.exists():
        return {"sources": [], "cards": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise HTTPException(503, "研究材料暂时无法读取")
    return {"sources": state.get("sources", []), "cards": state.get("cards", [])}


def _highlight_page(run_id: str, ref: str, text: str, start: int, end: int) -> str:
    """把快照渲染成"被引那段高亮"的只读 HTML 页（引用深链的本地侧）。

    刻意做成自包含的小页面：不引任何外部资源、不执行任何脚本——它是给"核回原文"用的，
    不需要也不应该有能力去联网。
    """
    head = html.escape(text[:start])
    hot = html.escape(text[start:end])
    tail = html.escape(text[end:])
    line = text[:start].count("\n") + 1
    return (
        "<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(ref)} 原文快照</title>"
        "<style>"
        "body{font:14px/1.7 -apple-system,Segoe UI,sans-serif;margin:0;padding:24px;background:#0d1117;color:#d7dde5}"
        ".head{position:sticky;top:0;background:#0d1117;padding-bottom:12px;border-bottom:1px solid #21313f;margin-bottom:16px}"
        ".num{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#8b98a5;font-size:12px}"
        "pre{white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;margin:0}"
        "mark{background:#ffd33d;color:#111;padding:1px 2px;border-radius:2px}"
        "</style></head><body>"
        f"<div class=\"head\"><div>原文快照 <b>{html.escape(ref)}</b>（{run_id}）</div>"
        f"<div class=\"num\">被引区间 chars={start}-{end}（第 {line} 行起，共 {end - start} 字符）"
        "　·　本页不再联网，内容与本次运行保存的完全一致</div></div>"
        f"<pre>{head}<mark id=\"cited\">{hot}</mark>{tail}</pre>"
        "</body></html>"
    )


@router.get("/api/runs/{run_id}/sources/{ref}", response_class=PlainTextResponse)
def api_run_source(run_id: str, ref: str, hl: str | None = None, *, store: StoreDep):
    """读该运行**已保存**的原文快照；不重新联网、不用新抓的网页冒充旧原文。

    带 ``?hl=a-b`` 时返回一个把 ``chars=a-b`` 高亮出来的 HTML 视图（引用深链的本地那一半：
    数据全是我们自己的，100% 可靠）。不带参数时保持原样——纯文本，供程序与既有测试消费。
    """
    _require_valid_run_id(run_id)
    if store.get_meta(run_id) is None:
        raise HTTPException(404, "run 不存在")
    text = store.read_source_text(run_id, ref)
    if text is None:
        raise HTTPException(404, "该来源不在这个运行里")
    if hl is not None:
        match = re.fullmatch(r"(\d+)-(\d+)", hl.strip())
        if match is None:
            raise HTTPException(400, 'hl 形如 "120-380"（字符区间）')
        start, end = int(match.group(1)), int(match.group(2))
        if start > end or end > len(text):
            raise HTTPException(400, "hl 区间超出该快照范围")
        return HTMLResponse(_highlight_page(run_id, ref, text, start, end))
    return text


@router.get("/api/runs/{run_id}/events", response_class=PlainTextResponse)
def api_run_events(run_id: str, compact: bool = False, *, store: StoreDep) -> str:
    _require_valid_run_id(run_id)
    try:
        text = store.read_events_text(run_id)
    except IncompatibleEventSchemaError as exc:
        raise HTTPException(409, str(exc)) from exc
    if text is None:
        raise HTTPException(404, "run 无事件记录")
    if compact:
        try:
            return "".join(json.dumps(_compact_event(json.loads(line)), ensure_ascii=False) + "\n"
                           for line in text.splitlines() if line.strip())
        except (ValueError, AttributeError, TypeError) as exc:
            raise HTTPException(503, "事件记录损坏，无法读取过程") from exc
    return text


@router.get("/api/runs/{run_id}/events/{seq}")
def api_run_event_detail(run_id: str, seq: int, *, store: StoreDep) -> dict:
    text = api_run_events(run_id, store=store)
    try:
        for line in text.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if isinstance(event, dict) and event.get("seq") == seq:
                return store.tool_event_detail(run_id, event)
    except (OSError, ValueError) as exc:
        raise HTTPException(503, "事件记录损坏，无法读取调用详情") from exc
    raise HTTPException(404, "这条调用记录不存在")
