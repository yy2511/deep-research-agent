"""在官方仓库目录里运行官方 run_evaluation.py，只把请求方式换成流式。

原因：评分中转站前有 Cloudflare，上游 100 秒内没有响应就返回 524；官方客户端不开流式，
GPT-5.5 判一批 50 条检查项通常要超过 100 秒。这里改成流式接收，拼回完整文字和用量，
交还给官方代码的数据结构完全相同；提示词、分批、校验、重试和结果文件都不变。

由 judge.py 以官方仓库为工作目录调用，参数原样转给官方 main()。
"""
import json
import os
import sys

import requests

sys.path.insert(0, os.getcwd())  # 官方模块在工作目录（官方仓库）里
import gpt_client  # noqa: E402
import run_evaluation  # noqa: E402


def _sse_lines(response):
    """只按 \n 切行再按 UTF-8 解码。requests 的 iter_lines(decode_unicode=True) 会把
    U+2028 等 Unicode 分隔符也当换行，模型输出里带这类字符时一行 JSON 会被切断。"""
    buffer = b""
    for block in response.iter_content(chunk_size=8192):
        buffer += block
        *lines, buffer = buffer.split(b"\n")
        for line in lines:
            yield line.rstrip(b"\r").decode("utf-8")
    if buffer.strip():
        yield buffer.rstrip(b"\r").decode("utf-8")


def _query_streaming(self, input_data):
    payload = {
        "model": self.model,
        "messages": [{"role": "user", "content": input_data.text}],
        "reasoning_effort": self.reasoning_effort,
        "max_completion_tokens": self.max_output_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if self.verbose:
        print(f"[request] model={self.model}, text_length={len(input_data.text)}, stream=True")
    response = requests.post(self.api_url, json=payload, stream=True, timeout=self.timeout,
                             headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
    if response.status_code >= 400:
        print(f"[err] HTTP {response.status_code} body: {response.text[:300]}")
        response.raise_for_status()
    parts, usage = [], {}
    for line in _sse_lines(response):
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        chunk = json.loads(line[6:])
        for choice in chunk.get("choices") or []:
            parts.append((choice.get("delta") or {}).get("content") or "")
        usage = chunk.get("usage") or usage
    details = usage.get("completion_tokens_details") or {}
    return gpt_client.GPTOutput(
        text="".join(parts),
        usage_metadata={
            "promptTokenCount": usage.get("prompt_tokens", 0),
            "candidatesTokenCount": usage.get("completion_tokens", 0),
            "totalTokenCount": usage.get("total_tokens", 0),
            "thoughtsTokenCount": details.get("reasoning_tokens", 0),
        },
        raw_response={"usage": usage},
    )


gpt_client.GPTClient.query = _query_streaming

if __name__ == "__main__":
    sys.argv[0] = "run_evaluation.py"
    run_evaluation.main()
