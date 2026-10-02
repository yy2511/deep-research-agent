"""用 BrowseComp-Plus 官方标准 Agent（search_agent/glm_zai_client.py）跑我们选的模型，作同模型对照组。

    JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home \\
      uv run --no-project --python 3.11 --with pyserini==1.2.0 --with rich --with python-dotenv \\
      python eval/bcp/run_official.py --ids 263 --batch official-<批次>

官方这个脚本是通用的 OpenAI 兼容 chat.completions + 函数调用 Agent（GLM-4.6 基线用它），
只是把服务地址写死为智谱。这里不改官方代码，只在启动时替换三处外部依赖：
- 服务地址与密钥：换成 .env 里的 OPENCODE_BASE_URL / OPENCODE_API_KEY，并按 opencode 要求带会话头；
- 分词器：官方从 Hugging Face 取 Qwen/Qwen3-0.6B，这里用本地 data/qwen3-tokenizer/（同一份文件）；
- 向量检索模块：只用 BM25，用空壳代替 faiss_searcher，免装 faiss/tevatron。
其余（BM25 索引、每次 5 条、摘要 512 token、题目模板、最多 100 轮）照官方默认；输出总预算按官方
文档对“话多的模型”的建议调到 40000（默认 20000，思考内容也算在内）。

结果写到 ``eval/bcp/runs/<批次>/run_*.json``（官方格式），可直接用 judge.py 评分。
"""
from __future__ import annotations

import argparse
import os
import sys
import types
from pathlib import Path

from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OFFICIAL = HERE / ".official"
TOKENIZER = HERE / "data" / "qwen3-tokenizer"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--ids", nargs="+", required=True, help="题号")
    p.add_argument("--batch", required=True, help="输出批次目录名（建议以 official- 开头）")
    p.add_argument("--model", default="deepseek-v4.1-flash")
    p.add_argument("--max-tokens", type=int, default=40000)
    p.add_argument("--threads", type=int, default=1, help="同时跑几题（官方脚本自带线程并行）")
    args = p.parse_args()

    values = dotenv_values(ROOT / ".env")
    base, key = values.get("OPENCODE_BASE_URL"), values.get("OPENCODE_API_KEY")
    if not base or not key:
        raise SystemExit("缺少 OPENCODE_BASE_URL / OPENCODE_API_KEY，请在项目 .env 中配置")

    out = HERE / "runs" / args.batch
    out.mkdir(parents=True, exist_ok=True)
    wanted = set(args.ids)
    rows = [line for line in (HERE / "data" / "queries.tsv").read_text(encoding="utf-8").splitlines()
            if line.split("\t", 1)[0] in wanted]
    if len(rows) != len(wanted):
        raise SystemExit("有题号不在 queries.tsv 里")
    tsv = out / "queries.tsv"
    tsv.write_text("\n".join(rows) + "\n", encoding="utf-8")

    # Pyserini 导入时会创建 OpenAI 客户端（不用），给占位值；官方脚本检查 ZAI_API_KEY 是否存在。
    os.environ.setdefault("OPENAI_API_KEY", "unused-by-bm25")
    os.environ["ZAI_API_KEY"] = "replaced-below"
    sys.path[:0] = [str(OFFICIAL / "search_agent"), str(OFFICIAL)]

    stub = types.ModuleType("searcher.searchers.faiss_searcher")
    stub.FaissSearcher = stub.ReasonIrSearcher = type("Unavailable", (), {})
    sys.modules["searcher.searchers.faiss_searcher"] = stub

    import transformers
    original = transformers.AutoTokenizer.from_pretrained
    transformers.AutoTokenizer.from_pretrained = lambda name, *a, **kw: original(
        str(TOKENIZER) if name == "Qwen/Qwen3-0.6B" else name, *a, **kw)

    import threading

    import openai
    import glm_zai_client  # 官方脚本

    def make_client(**_kw):
        """opencode 要求请求带 x-opencode-session（按会话路由、利于缓存）；与产品一样给每段对话
        一个固定编号。官方脚本每个线程顺序跑题，按线程区分即可。"""
        client = openai.OpenAI(api_key=key, base_url=base)
        create = client.chat.completions.create

        def create_with_session(**kwargs):
            session = f"bcp-official-{args.batch}-{threading.get_ident()}"
            return create(**kwargs, extra_headers={"x-opencode-session": session})

        client.chat.completions.create = create_with_session
        return client

    glm_zai_client.OpenAI = make_client

    sys.argv = ["glm_zai_client.py", "--query", str(tsv), "--model", args.model,
                "--max_tokens", str(args.max_tokens), "--output-dir", str(out),
                "--num-threads", str(args.threads),
                "--searcher-type", "bm25", "--index-path", str(HERE / "data" / "indexes" / "bm25")]
    glm_zai_client.main()


if __name__ == "__main__":
    main()
