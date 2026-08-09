"""拉 arxiv 摘要子集做 V2-1 local RAG 语料。

为什么自己写而不用 HF datasets:
- arxiv 官方 Atom API 无需 API key、无新依赖(标准库 urllib),可控可复跑。
- 主题要贴本项目(RAG/Agent/LLM),用 abs 关键词 + cs.CL/cs.IR/cs.AI 类别过滤。
- 一次拉够存 jsonl,后续 RAG 管道从本地读,不重复联网。

用法:
  uv run python scripts/fetch_arxiv_corpus.py --per-topic 250
  uv run python scripts/fetch_arxiv_corpus.py --per-topic 250 --out data/corpus/arxiv/docs.jsonl

arxiv API 礼貌约束:批量请求间隔 ≥3s,单次 max_results ≤200(官方建议)。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

# 三个子主题,各拉一批。关键词对准「项目要能回答的 seed query 方向」。
# - RAG:检索增强生成(核心)
# - Agent:LLM Agent / tool use / planning
# - LLM:大模型基础(eval/alignment/efficiency)
TOPICS: dict[str, str] = {
    "rag": '(abs:"retrieval-augmented generation" OR abs:"retrieval augmented" OR abs:RAG)',
    "agent": '(abs:"language agent" OR abs:"LLM agent" OR abs:"tool use" OR abs:agent)',
    "llm": '(abs:"large language model" OR abs:"LLM")',
}
CATEGORY_FILTER = "(cat:cs.CL OR cat:cs.IR OR cat:cs.AI)"


def fetch_topic(topic: str, query: str, per_topic: int) -> list[dict]:
    """拉单个主题的 arxiv 摘要,分页每页 100 条(arxiv 单次上限 200,保守取 100)。"""
    full = f"({query}) AND {CATEGORY_FILTER}"
    base = "http://export.arxiv.org/api/query"
    results: list[dict] = []
    start = 0
    page = 100
    while start < per_topic:
        n = min(page, per_topic - start)
        params = urllib.parse.urlencode({
            "search_query": full,
            "start": start,
            "max_results": n,
            "sortBy": "relevance",
            "sortOrder": "descending",
        })
        url = f"{base}?{params}"
        print(f"  [{topic}] start={start} fetch {n} ...", flush=True)
        req = urllib.request.Request(url, headers={"User-Agent": "dra-research-agent/0.1"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
        root = ET.fromstring(body)
        entries = root.findall("a:entry", NS)
        if not entries:
            print(f"  [{topic}] 无更多结果,提前停于 {start}", flush=True)
            break
        for e in entries:
            aid = e.find("a:id", NS).text.split("/abs/")[-1]
            title = " ".join(e.find("a:title", NS).text.split())
            summary = " ".join(e.find("a:summary", NS).text.split())
            cats = [c.attrib.get("term") for c in e.findall("a:category", NS)]
            published = (e.find("a:published", NS).text or "")[:10]
            results.append({
                "id": aid,
                "title": title,
                "abstract": summary,
                "categories": cats,
                "published": published,
                "topic_label": topic,  # 拉取主题标签,辅助 relevance 粗标注
            })
        start += n
        time.sleep(3)  # arxiv 礼貌延迟
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-topic", type=int, default=250, help="每个主题拉多少篇")
    ap.add_argument("--out", default="data/corpus/arxiv/docs.jsonl")
    args = ap.parse_args()

    all_docs: list[dict] = []
    seen_ids: set[str] = set()
    for topic, query in TOPICS.items():
        docs = fetch_topic(topic, query, args.per_topic)
        for d in docs:
            if d["id"] in seen_ids:
                continue  # 主题间会有重叠,按 arxiv id 去重
            seen_ids.add(d["id"])
            all_docs.append(d)
        print(f"[{topic}] 拉到 {len(docs)} 篇,累计去重后 {len(all_docs)} 篇", flush=True)

    with open(args.out, "w", encoding="utf-8") as f:
        for d in all_docs:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"\n✅ 落盘 {len(all_docs)} 篇到 {args.out}")


if __name__ == "__main__":
    main()
