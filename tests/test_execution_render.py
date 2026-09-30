import json
from pathlib import Path
import pytest
from dra.execution import ExecutionConfig,ExecutionSession,MiB
from dra.artifacts import validate_file


@pytest.mark.docker
def test_chinese_report_long_table_and_editable_mindmap(tmp_path):
    s=ExecutionSession(tmp_path,'render',ExecutionConfig(enabled=True))
    try:
        body='# 研究报告：中文与长表\n\n这份报告验证中文字体、分页表格、引用和多格式文件。 [c1]\n\n## 数据表\n\n| 项目 | 数值 | 说明 |\n|---|---|---|\n'
        body+=''.join(f'| 条目 {i} | {i*3} | 保留完整中文说明，验证分页后的表头。 |\n' for i in range(1,65))
        s.tool('write_file',{'path':'/workspace/work/report.md','content':body})
        tree={'title':'研究方法','children':[{'title':'资料收集','children':[{'title':'公开网页'},{'title':'用户资料'}]},{'title':'证据核对','children':[{'title':'原文定位'},{'title':'Jev 支持判断'}]},{'title':'输出与复核'}]}
        s.tool('write_file',{'path':'/workspace/work/map.json','content':json.dumps(tree,ensure_ascii=False)})
        receipt=s.tool('bash',{'command':'dra-render report --input /workspace/work/report.md --html /workspace/output/report.html --pdf /workspace/output/report.pdf && dra-render mindmap --input /workspace/work/map.json --svg /workspace/output/map.svg --png /workspace/output/map.png --json /workspace/output/map.json','timeout_s':90})
        assert receipt['ok'],receipt
        files=s.snapshot({f'/workspace/output/{name}':20*MiB for name in ['report.html','report.pdf','map.svg','map.png','map.json']})
        for path,data in files.items():
            validate_file(Path(path).name,data)
            (tmp_path/Path(path).name).write_bytes(data)
        from pypdf import PdfReader
        pdf=PdfReader(tmp_path/'report.pdf')
        assert len(pdf.pages)>=2
        text=''.join(p.extract_text() for p in pdf.pages)
        assert '研究报告' in text and '条目 64' in text and '[c1]' in text
        assert json.loads(files['/workspace/output/map.json'])==tree
    finally:s.close()
