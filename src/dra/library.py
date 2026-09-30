"""个人资料库：不可变版本、串行导入、固定范围的本地混合检索。

资料/索引以版本为单位发布，运行只持有版本 ID。移出库不删除历史原件。
不在请求失败时下载模型或切换到网络检索。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dra.models import RetrievedDoc
from dra.paths import DATA_DIR, PROJECT_ROOT

MAX_BYTES = 10 * 1024 * 1024
MAX_CHARS = 200_000
CHUNK_CONFIG = {"window": 300, "overlap": 40, "version": 1}
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+|[一-鿿]")


def tokenize(text: str) -> list[str]:
    """BM25 分词：英文 / 数字连续段与中文单字，与已有资料索引保持一致。"""
    if not text:
        return []
    return _TOKEN_PATTERN.findall(text.lower())


class LibraryError(ValueError):
    pass


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(path: Path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def chunk_ranges(text: str, offsets: list, window=300, overlap=40) -> list[tuple[int, int]]:
    """按真实 tokenizer 偏移切块，在窗口后半段优先段落边界；坐标不清洗不重建。"""
    if not 0 <= overlap < window:
        raise ValueError("invalid chunk overlap")
    offsets = [(a, b) for a, b in offsets if b > a]
    if not offsets:
        return [(0, len(text))] if text else []
    out, index = [], 0
    while index < len(offsets):
        stop = min(index + window, len(offsets))
        start = offsets[index][0] if index else 0
        end = offsets[stop][0] if stop < len(offsets) else len(text)
        if stop < len(offsets):
            boundary = text.rfind("\n\n", offsets[index + (stop - index) // 2][0], end)
            if boundary > start:
                while stop > index + overlap + 1 and offsets[stop - 1][0] >= boundary:
                    stop -= 1
                end = offsets[stop][0]
        out.append((start, end))
        if stop == len(offsets):
            break
        index = max(index + 1, stop - overlap)
    return out


class LocalEncoder:
    """仅加载已有模型；编码与可选重排串行，避免本机多个线程争抢内存。"""
    def __init__(self):
        default_model = PROJECT_ROOT / "data/models/ms_cache/BAAI/bge-m3"
        self.path = Path(os.environ.get("DRA_EMBEDDING_MODEL", str(default_model))).expanduser()
        self.lock = threading.RLock()
        self.model = None
        self.reranker = None
        if not self.path.is_dir() or any(importlib.util.find_spec(name) is None for name in ("sentence_transformers", "numpy", "rank_bm25")):
            raise LibraryError("本地检索尚未配置：安装 local-rag 依赖，并通过 DRA_EMBEDDING_MODEL 指定已有 BGE-M3 目录")
        if not (self.path / "config.json").is_file():
            raise LibraryError("本地 BGE-M3 模型目录不完整")
        identity = [(p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(self.path.iterdir()) if p.is_file()]
        self.signature = "bge-m3:" + _hash(json.dumps(identity).encode())

    def _load(self):
        if self.model is None:
            from sentence_transformers import SentenceTransformer
            self.model = SentenceTransformer(str(self.path), local_files_only=True)
            self.model.max_seq_length = 512
        return self.model

    def offsets(self, text):
        with self.lock:
            return self._load().tokenizer(text, add_special_tokens=False, return_offsets_mapping=True,
                                          truncation=False, verbose=False)["offset_mapping"]

    def encode(self, texts):
        with self.lock:
            model = self._load()
            lengths = model.tokenizer(texts, add_special_tokens=True, truncation=False, return_length=True)["length"]
            if max(lengths, default=0) > 512:
                raise LibraryError("检索文字超过模型窗口，请缩短查询或检查切块配置")
            return model.encode(texts, batch_size=8, normalize_embeddings=True,
                                       show_progress_bar=False, convert_to_numpy=True)

    def rerank(self, query, texts):
        with self.lock:
            if self.reranker is None:
                from sentence_transformers import CrossEncoder
                path = Path(os.environ.get("DRA_RERANKER_MODEL", str(self.path.parent / "bge-reranker-v2-m3")))
                if not path.is_dir():
                    raise LibraryError("没有配置本地重排模型；请关闭重排或配置 DRA_RERANKER_MODEL")
                self.reranker = CrossEncoder(str(path), local_files_only=True, max_length=512)
            pairs = [(query, t) for t in texts]
            lengths = self.reranker.tokenizer(pairs, truncation=False, return_length=True)["length"]
            if max(lengths, default=0) > 512:
                raise LibraryError("查询与候选超过重排窗口，请缩短查询或关闭重排")
            return self.reranker.predict(pairs, batch_size=4)


class LibraryStore:
    def __init__(self, root: Path, *, encoder=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "versions").mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self._encoder = encoder
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="library-import")
        self.slots = threading.BoundedSemaphore(4)
        self.state = json.loads((self.root / "manifest.json").read_text()) if (self.root / "manifest.json").exists() else {"generation": 0, "documents": {}, "versions": {}}
        for version in self.state["versions"].values():
            if version["status"] in {"queued", "parsing", "indexing"}:
                version.update(status="failed", error="导入因服务重启中断，请重新上传重试")
        self._commit()

    def close(self):
        self.pool.shutdown(wait=True)

    @property
    def encoder(self):
        with self.lock:
            if self._encoder is None:
                self._encoder = LocalEncoder()
            return self._encoder

    def _commit(self):
        _json(self.root / "manifest.json", self.state)

    def _version(self, version_id):
        with self.lock:
            version = self.state["versions"].get(version_id)
            if not version:
                raise LibraryError("资料版本不存在")
            return dict(version)

    def list_documents(self):
        with self.lock:
            docs = []
            for doc in self.state["documents"].values():
                if doc["removed"]:
                    continue
                docs.append({**doc, "current": self._version(doc["current_version"]) if doc["current_version"] else None,
                             "latest": self._version(doc["latest_version"])})
            return {"documents": docs, "generation": self.state["generation"],
                    "limits": {"bytes": MAX_BYTES, "chars": MAX_CHARS, "pages": 200}}

    def import_bytes(self, name: str, body: bytes, *, replace: str | None = None, wait=False):
        suffix = Path(name).suffix.lower()
        if name != Path(name).name or "\\" in name or len(name) > 180 or any(ord(c) < 32 for c in name):
            raise LibraryError("文件名非法")
        if suffix not in {".txt", ".md", ".pdf"}:
            raise LibraryError("仅支持 TXT、Markdown 和文本 PDF")
        if not body or len(body) > MAX_BYTES:
            raise LibraryError("文件不能为空，且不能超过 10 MiB")
        if suffix != ".pdf" and b"\x00" in body:
            raise LibraryError("文件含二进制内容")
        digest = _hash(body)
        with self.lock:
            if replace and (replace not in self.state["documents"] or self.state["documents"][replace]["removed"]):
                raise LibraryError("待更新的资料不存在")
            for version in self.state["versions"].values():
                doc = self.state["documents"][version["document_id"]]
                if (version["original_sha256"] == digest and version["suffix"] == suffix
                        and version["status"] in {"ready", "queued", "parsing", "indexing"}
                        and not doc["removed"] and (not replace or version["document_id"] == replace)
                        and version["version_id"] == doc["latest_version"]):
                    if version["status"] != "ready" or (
                        version.get("encoder") == self.encoder.signature
                        and version.get("chunk_config") == CHUNK_CONFIG
                        and version.get("parser") == ("pypdf-text-v1" if suffix == ".pdf" else "utf8-verbatim-v1")
                    ):
                        return dict(version)
            if not self.slots.acquire(blocking=False):
                raise LibraryError("导入队列已满，请等待当前文件处理完毕")
            document_id = replace or "d" + uuid.uuid4().hex
            version_id = "v" + uuid.uuid4().hex
            directory = self.root / "versions" / version_id
            try:
                directory.mkdir()
                (directory / "original").write_bytes(body)
                version = {"document_id": document_id, "version_id": version_id, "file_name": name,
                           "suffix": suffix, "original_sha256": digest, "size": len(body),
                           "status": "queued", "error": None, "created_at": time.time()}
                self.state["versions"][version_id] = version
                doc = self.state["documents"].setdefault(document_id, {"document_id": document_id,
                               "current_version": None, "removed": False})
                doc["latest_version"] = version_id
                self._commit()
            except BaseException:
                self.slots.release()
                raise
            future = self.pool.submit(self._ingest, version_id)
        if wait:
            future.result(timeout=180)
            return self._version(version_id)
        return dict(version)

    def _status(self, version_id, **changes):
        with self.lock:
            self.state["versions"][version_id].update(changes)
            self._commit()

    def _ingest(self, version_id):
        directory = self.root / "versions" / version_id
        try:
            self._status(version_id, status="parsing")
            version = self._version(version_id)
            with subprocess.Popen([sys.executable, "-m", "dra.document_parse", str(directory / "original"),
                                   version["suffix"], str(directory / "parsed.json")],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
                deadline = time.monotonic() + 45
                try:
                    while process.poll() is None:
                        if time.monotonic() > deadline:
                            raise LibraryError("解析超过 45 秒，请拆分文件")
                        if sys.platform == "darwin":
                            rss = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(process.pid)],
                                                 capture_output=True, text=True, timeout=2).stdout.strip()
                            if rss.isdigit() and int(rss) > 1024 * 1024:
                                raise LibraryError("解析内存超过 1 GiB，请拆分文件")
                        time.sleep(0.1)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                if process.returncode:
                    raise LibraryError("解析进程退出：文件损坏或超过内存/时间限制")
            parsed = json.loads((directory / "parsed.json").read_text())
            if parsed.get("error"):
                raise LibraryError(parsed["error"])
            text = parsed.pop("text")
            (directory / "text.txt").write_bytes(text.encode("utf-8"))
            self._status(version_id, status="indexing")
            encoder = self.encoder
            ranges = chunk_ranges(text, encoder.offsets(text), **{k: CHUNK_CONFIG[k] for k in ("window", "overlap")})
            chunks = [{"id": _hash(f"{version_id}:{a}:{b}:{CHUNK_CONFIG}".encode()), "start": a, "end": b} for a, b in ranges]
            import numpy as np
            vectors = np.asarray(encoder.encode([text[a:b] for a, b in ranges]), dtype=np.float32)
            if vectors.ndim != 2 or len(vectors) != len(chunks) or not np.isfinite(vectors).all():
                raise LibraryError("向量生成不完整，未发布索引")
            vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
            np.save(directory / "vectors.npy", vectors, allow_pickle=False)
            _json(directory / "chunks.json", chunks)
            with self.lock:
                previous_current = self.state["documents"][version["document_id"]]["current_version"]
                previous_generation = self.state["generation"]
                self.state["versions"][version_id].update(status="ready", error=None, raw_chars=len(text),
                    text_sha256=_hash(text.encode()), encoder=encoder.signature, chunks=len(chunks),
                    chunk_config=CHUNK_CONFIG, **parsed)
                doc = self.state["documents"][version["document_id"]]
                doc["current_version"] = version_id
                self.state["generation"] += 1
                try:
                    self._commit()
                except Exception:
                    doc["current_version"] = previous_current
                    self.state["generation"] = previous_generation
                    raise
        except Exception as exc:
            message = str(exc) if isinstance(exc, LibraryError) else f"导入失败（{type(exc).__name__}），请检查本地模型和文件后重试"
            self._status(version_id, status="failed", error=message)
        finally:
            self.slots.release()

    def remove(self, document_id):
        with self.lock:
            doc = self.state["documents"].get(document_id)
            if not doc:
                raise LibraryError("资料不存在")
            doc["removed"] = True
            self.state["generation"] += 1
            self._commit()

    def pin(self, document_ids):
        if not document_ids or len(document_ids) > 50 or len(set(document_ids)) != len(document_ids):
            raise LibraryError("请选取 1–50 份不同的可用资料")
        with self.lock:
            versions = []
            for key in document_ids:
                doc = self.state["documents"].get(key)
                if not doc or doc["removed"] or not doc["current_version"]:
                    raise LibraryError("所选资料不可用，请刷新资料列表")
                versions.append(doc["current_version"])
            return {"generation": self.state["generation"], "version_ids": versions,
                    "files": [self._version(v)["file_name"] for v in versions]}

    def read_version(self, version_id):
        version = self._version(version_id)
        if version["status"] != "ready":
            raise LibraryError("该资料尚未完成索引")
        data = (self.root / "versions" / version_id / "text.txt").read_bytes()
        if _hash(data) != version["text_sha256"]:
            raise LibraryError("资料文本校验失败，不能作为引用依据")
        return {**version, "text": data.decode("utf-8")}

    def validate_selection(self, selection):
        """追问继承固定版本；新版不替换它，已移除/损坏/失配的版本不能重用。"""
        with self.lock:
            ids = selection.get("version_ids", [])
            if not ids:
                raise LibraryError("本次研究的固定资料为空")
            for version_id in ids:
                version = self.read_version(version_id)
                doc = self.state["documents"].get(version["document_id"])
                if not doc or doc.get("removed"):
                    raise LibraryError("本次研究的资料已移出资料库，请新建研究选择可用资料")
                self.original(version_id)
                directory = self.root / "versions" / version_id
                if any(not (directory/name).exists() for name in ("chunks.json","vectors.npy")):
                    raise LibraryError("固定版本索引缺失，请新建研究选择可用资料")
                if version["encoder"] != self.encoder.signature:
                    raise LibraryError("固定版本索引模型不一致，请重新导入资料")
        return selection

    def original(self, version_id):
        version = self._version(version_id)
        data = (self.root / "versions" / version_id / "original").read_bytes()
        if _hash(data) != version["original_sha256"]:
            raise LibraryError("资料原件校验失败")
        return data

    def search(self, query, selection, *, rerank=False, method="hybrid"):
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise LibraryError("资料查询应为 1–2000 个字符")
        import numpy as np
        from rank_bm25 import BM25Okapi

        candidates, vectors = [], []
        versions = {}
        encoder = self.encoder
        for version_id in selection.get("version_ids", []):
            version = self.read_version(version_id)
            if version["encoder"] != encoder.signature:
                raise LibraryError("索引模型与当前模型不一致，请重新导入资料")
            versions[version_id] = version
            directory = self.root / "versions" / version_id
            chunks = json.loads((directory / "chunks.json").read_text())
            for chunk in chunks:
                candidates.append({**chunk, "version_id": version_id,
                                   "text": version["text"][chunk["start"]:chunk["end"]]})
            vectors.append(np.load(directory / "vectors.npy", allow_pickle=False))
        if not candidates:
            return []
        q = np.asarray(encoder.encode([query]), dtype=np.float32)[0]
        q /= max(np.linalg.norm(q), 1e-12)
        dense = (np.concatenate(vectors) @ q).argsort()[::-1][:20]
        scores = {int(i): 1 / (60 + rank) for rank, i in enumerate(dense, 1)}
        if method != "dense":
            tokens = [tokenize(c["text"]) or [""] for c in candidates]
            # BM25Okapi 在小语料的高频词上可能为负；仍按名次融合，不混原始分数。
            sparse = np.asarray(BM25Okapi(tokens).get_scores(tokenize(query))).argsort()[::-1][:20]
            for rank, i in enumerate(sparse, 1):
                scores[int(i)] = scores.get(int(i), 0) + 1 / (60 + rank)
        ordered = sorted(scores, key=lambda i: scores[i], reverse=True)[:20]
        if rerank:
            ranks = encoder.rerank(query, [candidates[i]["text"] for i in ordered])
            ordered = [i for _, i in sorted(zip(ranks, ordered), reverse=True)]
        grouped = {}
        for index in ordered[:8]:
            hit = candidates[index]
            grouped.setdefault(hit["version_id"], []).append({**hit, "score": scores[index]})
        docs = []
        for version_id, matches in grouped.items():
            v = versions[version_id]
            metadata = {k: v[k] for k in ("document_id", "version_id", "file_name", "suffix", "pages", "warnings", "original_sha256", "text_sha256")}
            metadata["matches"] = matches
            docs.append(RetrievedDoc(title=v["file_name"], snippet=matches[0]["text"], document=metadata))
        return docs


_library: LibraryStore | None = None
_library_lock = threading.Lock()


def get_library() -> LibraryStore:
    global _library
    with _library_lock:
        if _library is None:
            _library = LibraryStore(DATA_DIR / "library")
        return _library
