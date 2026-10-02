"""BrowseComp-Plus 文档库的本机检索服务（BM25），供研究 Agent 在评测时代替联网搜索。

Pyserini 依赖 torch 和 Java 21，体积大，不进项目依赖；用 uv 临时环境单独起一个进程
（Java 21：brew install openjdk@21，与其他版本并存）：

    JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home \\
        uv run --no-project --python 3.11 --with pyserini==1.2.0 python eval/bcp/search_server.py

接口（只听 127.0.0.1）：
    GET /search?q=...&k=5   → {"hits": [{"docid", "score", "snippet", "text"}]}，text 是全文
    GET /doc?id=...         → {"docid", "text"}

检索与官方 searcher/searchers/bm25_searcher.py 相同：官方预建索引 + LuceneSearcher 默认参数。
摘要与官方 searcher/tools.py 相同：用 Qwen3-0.6B 的分词器取全文前 512 个 token（只用分词规则，
不跑模型）；分词器文件放在 data/qwen3-tokenizer/（从 Hugging Face Qwen/Qwen3-0.6B 下载）。
"""
from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlparse

# Pyserini 导入时会顺带创建 OpenAI 客户端（给它的 OpenAI 编码器用），没有 key 就报错。
# BM25 用不到它；给本进程一个占位值，不读、不传真实密钥。
os.environ["OPENAI_API_KEY"] = "unused-by-bm25"
from pyserini.search.lucene import LuceneSearcher  # noqa: E402
from transformers import AutoTokenizer  # noqa: E402

HERE = Path(__file__).resolve().parent


PREFIX_CHARS = 20_000
PREFIX_MARGIN = 64


def make_snippet(tokenizer, text: str, max_tokens: int) -> str:
    """官方做法：超过上限才截断，截断后按 token 解码回文字。

    长文整篇分词很慢（单次搜索 0.5 秒）。前 max_tokens 个 token 只取决于开头，所以先只分
    开头 2 万字符：分出的 token 比上限多出一截余量时，前 max_tokens 个与整篇分词相同
    （切口附近的 token 才可能不同）；余量不够再退回整篇分词。"""
    head = text[:PREFIX_CHARS]
    tokens = tokenizer.encode(head, add_special_tokens=False)
    if len(text) > PREFIX_CHARS and len(tokens) <= max_tokens + PREFIX_MARGIN:
        tokens = tokenizer.encode(text, add_special_tokens=False)
    if len(tokens) <= max_tokens:
        return text
    return tokenizer.decode(tokens[:max_tokens], skip_special_tokens=True)


def make_handler(searcher: LuceneSearcher, tokenizer, snippet_tokens: int):
    lock = Lock()  # LuceneSearcher 经 JNI 调 Java，串行调用最稳妥

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            args = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/search" and args.get("q", "").strip():
                k = int(args.get("k", 5))
                with lock:
                    hits = searcher.search(args["q"], k)
                    texts = [json.loads(h.lucene_document.get("raw"))["contents"] for h in hits]
                rows = [{"docid": h.docid, "score": h.score, "snippet": make_snippet(tokenizer, text, snippet_tokens),
                         "text": text} for h, text in zip(hits, texts)]
                self._send(200, {"hits": rows})
            elif url.path == "/doc" and args.get("id"):
                with lock:
                    doc = searcher.doc(args["id"])
                if doc is None:
                    self._send(404, {"error": "文档不存在"})
                else:
                    self._send(200, {"docid": args["id"], "text": json.loads(doc.raw())["contents"]})
            else:
                self._send(400, {"error": "用法：/search?q=...&k=5 或 /doc?id=..."})

        def log_message(self, *_):  # 不刷屏
            pass

    return Handler


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--index", default=str(HERE / "data" / "indexes" / "bm25"))
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--tokenizer", default=str(HERE / "data" / "qwen3-tokenizer"))
    p.add_argument("--snippet-tokens", type=int, default=512)
    args = p.parse_args()
    searcher = LuceneSearcher(args.index)
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer)
    print(f"BM25 检索服务：http://127.0.0.1:{args.port}（{searcher.num_docs} 篇）", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(searcher, tokenizer, args.snippet_tokens)).serve_forever()


if __name__ == "__main__":
    main()
