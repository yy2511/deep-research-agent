"""下载固定版本的数据与官方评分提示词；仅下载，不调用模型。"""
from __future__ import annotations

import ast
import hashlib
import json
import textwrap
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REVISION = "b2623f8653065c2672de6d941fc5434cd652376c"
DATA_URL = f"https://huggingface.co/datasets/google/deepsearchqa/resolve/{REVISION}/DSQA-full.csv"
DATA_SHA256 = "25d48dcf7efa872e5467032e8b8eedf38d301f59a252d0da95cda584baa78396"
NOTEBOOK_URL = "https://www.kaggle.com/api/v1/kernels/pull/andrewmingwang/deepsearchqa-starter-code"
NOTEBOOK_SHA256 = "aa1ace0a1e0023a5cad5e662968bcf18c6b4f5c0666cbad78a46ba5f813559b2"


def extract_prompt(notebook: str) -> dict[str, str]:
    """只解析两个字符串常量，绝不执行下载的 notebook。"""
    names = {"DEEPSEARCH_QA_PROMPT", "GRADER_RATING_OUTPUT_EXAMPLE"}
    found = {}
    for cell in json.loads(notebook)["cells"]:
        source = "".join(cell.get("source", []))
        if cell["cell_type"] != "code" or "DEEPSEARCH_QA_PROMPT =" not in source:
            continue
        for node in ast.parse(source).body:
            if not isinstance(node, ast.Assign) or not isinstance(node.targets[0], ast.Name):
                continue
            name = node.targets[0].id
            if name not in names:
                continue
            value = node.value
            if isinstance(value, ast.Call):
                if ast.unparse(value.func) != "textwrap.dedent" or len(value.args) != 1:
                    raise ValueError("Unexpected prompt expression")
                found[name] = textwrap.dedent(ast.literal_eval(value.args[0]))
            else:
                found[name] = ast.literal_eval(value)
    if set(found) != names or not all(isinstance(v, str) for v in found.values()):
        raise ValueError("Missing official prompt constants")
    return found


def main():
    (HERE / "data").mkdir(parents=True, exist_ok=True)
    (HERE / ".official").mkdir(exist_ok=True)
    data_path, notebook_path = HERE / "data/DSQA-full.csv", HERE / ".official/starter.ipynb"
    data = data_path.read_bytes() if data_path.exists() else urllib.request.urlopen(DATA_URL, timeout=30).read()
    if notebook_path.exists():
        notebook = notebook_path.read_text()
    else:
        payload = json.load(urllib.request.urlopen(NOTEBOOK_URL, timeout=30))
        notebook = payload["blob"]["source"]
        (HERE / ".official/metadata.json").write_text(json.dumps(payload["metadata"], indent=2))
    if hashlib.sha256(data).hexdigest() != DATA_SHA256:
        raise ValueError("Dataset checksum changed")
    if hashlib.sha256(notebook.encode()).hexdigest() != NOTEBOOK_SHA256:
        raise ValueError("Official notebook changed; review before updating the pinned checksum")
    prompts = extract_prompt(notebook)
    data_path.write_bytes(data)
    notebook_path.write_text(notebook)
    (HERE / ".official/prompts.json").write_text(json.dumps(prompts, indent=2))
    provenance = {"dataset_revision": REVISION, "dataset_url": DATA_URL,
                  "dataset_sha256": DATA_SHA256, "notebook_url": NOTEBOOK_URL,
                  "notebook_sha256": NOTEBOOK_SHA256}
    (HERE / "data/provenance.json").write_text(json.dumps(provenance, indent=2))
    print(json.dumps(provenance))


if __name__ == "__main__":
    main()
