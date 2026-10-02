"""给 run_bcp.py 的结果打分：运行官方 evaluate_run.py，只把本地 vLLM 换成 API 调 Qwen3-32B。

    uv run --no-project --with numpy --with tqdm --with requests --with python-dotenv \\
        python eval/bcp/judge.py runs/20261002-pilot

官方脚本用本地显卡加载 Qwen3-32B；本机没有显卡，这里替换官方代码里的 vllm 模块，
让 LLM.chat 改走 .env 里 BCP_BASE_URL / BCP_API_KEY 的 qwen3-32b。评分提示词、采样参数
（温度 0.7、top_p 0.8、top_k 20、最多 4096 token）、答案解析、证据召回率与汇总都用官方代码。

与官方的唯一差别：官方用 enable_thinking=False 关思考，这个 API 不认该参数，改用千问自带的
软开关——在评分提示词末尾加 " /no_think"，模型输出空思考块后直接作答。

结果写到 ``eval/bcp/evals/<批次>/``：每题 ``<题号>_eval.json`` 和 ``evaluation_summary.json``。
"""
from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import requests
from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OFFICIAL = HERE / ".official"
OFFICIAL_REPO = "https://github.com/texttron/BrowseComp-Plus"
OFFICIAL_COMMIT = "046949032b0328319cc9a02663a759ec601d9402"
JUDGE_MODEL = "qwen3-32b"
NO_THINK = " /no_think"


def ensure_official() -> None:
    """取官方代码并切到固定版本；已有且版本对就不动。"""
    if not (OFFICIAL / ".git").exists():
        subprocess.run(["git", "clone", "--quiet", OFFICIAL_REPO, str(OFFICIAL)], check=True)
    head = subprocess.run(["git", "-C", str(OFFICIAL), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    if head != OFFICIAL_COMMIT:
        subprocess.run(["git", "-C", str(OFFICIAL), "fetch", "--quiet", "origin"], check=True)
        subprocess.run(["git", "-C", str(OFFICIAL), "checkout", "--quiet", OFFICIAL_COMMIT], check=True)


def api_vllm(base: str, key: str) -> types.ModuleType:
    """假的 vllm 模块：接口与官方用到的部分一致，内部调 API。"""

    class SamplingParams:
        def __init__(self, temperature, top_p, top_k, max_tokens):
            self.temperature, self.top_p, self.top_k, self.max_tokens = temperature, top_p, top_k, max_tokens

    class LLM:
        def __init__(self, model, tensor_parallel_size=1):
            self.model = model

        def _one(self, messages, params) -> str:
            messages = [*messages[:-1], {**messages[-1], "content": messages[-1]["content"] + NO_THINK}]
            body = {"model": JUDGE_MODEL, "messages": messages, "temperature": params.temperature,
                    "top_p": params.top_p, "top_k": params.top_k, "max_tokens": params.max_tokens}
            for attempt in range(3):
                try:
                    r = requests.post(f"{base}/chat/completions", json=body, timeout=180,
                                      headers={"Authorization": f"Bearer {key}"})
                    r.raise_for_status()
                    return r.json()["choices"][0]["message"].get("content") or ""
                except (requests.RequestException, KeyError, ValueError) as exc:
                    print(f"[评分请求失败，第 {attempt + 1} 次] {exc}", file=sys.stderr)
            return ""  # 官方解析拿到空文本会记为 parse_error，不算对

        def chat(self, messages_list, sampling_params, chat_template_kwargs=None):
            outputs = []
            for messages in messages_list:
                text = self._one(messages, sampling_params)
                outputs.append(types.SimpleNamespace(outputs=[types.SimpleNamespace(text=text)]))
            return outputs

    module = types.ModuleType("vllm")
    module.LLM, module.SamplingParams = LLM, SamplingParams
    return module


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        raise SystemExit("用法：judge.py runs/<批次>（相对 eval/bcp/）")
    run_dir = (HERE / argv[0]).resolve()
    values = dotenv_values(ROOT / ".env")
    base, key = values.get("BCP_BASE_URL"), values.get("BCP_API_KEY")
    if not base or not key:
        raise SystemExit("缺少 BCP_BASE_URL / BCP_API_KEY，请在项目 .env 中配置")
    ensure_official()
    sys.modules["vllm"] = api_vllm(base.rstrip("/"), key)
    sys.path.insert(0, str(OFFICIAL / "scripts_evaluation"))
    import evaluate_run  # noqa: E402  官方脚本

    sys.argv = ["evaluate_run.py", "--input_dir", str(run_dir), "--eval_dir", str(HERE / "evals"),
                "--ground_truth", str(HERE / "data" / "browsecomp_plus_decrypted.jsonl"),
                "--qrel_evidence", str(OFFICIAL / "topics-qrels" / "qrel_evidence.txt"),
                "--model", f"Qwen/Qwen3-32B（API {JUDGE_MODEL}，/no_think）"]
    evaluate_run.main()


if __name__ == "__main__":
    main()
