#!/opt/runtime/.venv/bin/python
"""确定性文件生成 helper，运行于受限容器；模型负责材料选择与内容。"""
import argparse
import importlib.util
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
helper_path=Path(__file__).with_name('helper.py')
if not helper_path.is_file():helper_path=Path('/opt/dra-tools/helper.py')
spec=importlib.util.spec_from_file_location('dra_helper',helper_path)
helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
parts=helper.parts

CSS='''@page { size: A4; margin: 19mm 17mm; @bottom-center { content: counter(page); font-size: 9pt; color: #777; } }
body { font-family: "Noto Sans CJK SC", sans-serif; color: #292c32; line-height: 1.7; font-size: 10pt; max-width: 960px; margin: 0 auto; padding: 24px; }
h1 { font-size: 25pt; color: #204e50; line-height: 1.35; } h2 { font-size: 16pt; color: #204e50; border-bottom: 1px solid #c9d7d7; padding-bottom: 5px; }
h1,h2,h3 { break-after: avoid; } table { border-collapse: collapse; width: 100%; font-size: 9pt; table-layout: fixed; }
th,td { border: 1px solid #cad4d6; padding: 6px; overflow-wrap: anywhere; } th { background: #edf3f3; } tr { break-inside: avoid; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #f3f5f5; padding: 12px; } code { font-family: monospace; }
img { max-width: 100%; } a { color: #25676a; overflow-wrap: anywhere; } blockquote { border-left: 3px solid #9bb7b8; padding-left: 14px; color: #526164; }
@media print { body { padding: 0; max-width: none; } }
'''


def path(value,write=False):
    root=Path(os.environ.get('DRA_WORKSPACE','/workspace'))
    logical='/workspace/'+value[len(str(root))+1:] if value.startswith(str(root)+'/') else value
    segments=parts(logical,write=write)
    p=root.joinpath(*segments)
    if not p.resolve().is_relative_to(root.resolve()):raise ValueError('工作区路径越界')
    return p


def clean_svg(text):
    text=re.sub(r'<!DOCTYPE[^>]*>','',text,flags=re.S)
    if '<!ENTITY' in text.upper():raise ValueError('不支持实体')
    root=ET.fromstring(text)
    for parent in root.iter():
        for node in list(parent):
            if node.tag.split('}')[-1]=='metadata':parent.remove(node)
    ET.register_namespace('','http://www.w3.org/2000/svg')
    ET.register_namespace('xlink','http://www.w3.org/1999/xlink')
    return ET.tostring(root,encoding='unicode')


def deny_fetch(url,*args,**kwargs):
    raise ValueError('PDF helper 禁止读取外部资源：'+url[:120])


def report(args):
    import markdown
    body=path(args.input).read_text(encoding='utf-8')
    if len(body.encode())>256*1024:raise ValueError('正文过大')
    if args.materials:
        data=json.loads(path(args.materials).read_text())
        sources={item['ref']:item for item in data.get('sources',[])}
        rows=[]
        for card in data.get('cards',[]):
            cid=card['card_id']
            if not re.search(r'\['+re.escape(cid)+r'\]',body):continue
            refs=list(dict.fromkeys([q.get('source_ref') for q in card.get('quotes',[])] or [card.get('source_ref')]))
            labels=[]
            for ref in refs:
                item=sources.get(ref,{})
                title=html.escape(item.get('title') or ref or '')
                url=item.get('url','')
                if re.match(r'^https?://',url):title=f'<a href="{html.escape(url,quote=True)}">{title}</a>'
                else:title+='（保存资料：'+html.escape(str((item.get('document') or {}).get('version_id') or ref))+ '）'
                labels.append(title)
            rows.append('<li><strong>'+html.escape(cid)+'</strong> '+ '；'.join(labels)+'</li>')
        bibliography='<h2>来源与证据</h2><ol>'+''.join(rows)+'</ol>' if rows else ''
    else:bibliography=''
    page='<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>'+html.escape(args.title)+'</title><style>'+CSS+'</style></head><body><main>'+markdown.markdown(body,extensions=['tables','fenced_code'])+bibliography+'</main></body></html>'
    if args.html:path(args.html,True).write_text(page,encoding='utf-8')
    if args.pdf:
        from weasyprint import HTML
        HTML(string=page,url_fetcher=deny_fetch).write_pdf(path(args.pdf,True))
    if not(args.html or args.pdf):raise ValueError('至少选择 --html 或 --pdf')


def mindmap(args):
    tree=json.loads(path(args.input).read_text());nodes=[];edges=[]
    def walk(item,parent=None,depth=0):
        if not isinstance(item,dict) or not isinstance(item.get('title'),str) or not 0<len(item['title'])<=150 or depth>12 or len(nodes)>=200:raise ValueError('导图节点无效或过多')
        ident=len(nodes);nodes.append(item['title'])
        if parent is not None:edges.append((parent,ident))
        children=item.get('children',[])
        if not isinstance(children,list):raise ValueError('children 须为数组')
        for child in children:walk(child,ident,depth+1)
    walk(tree)
    graph=['digraph G { graph [rankdir=LR, bgcolor="white", pad="0.3", nodesep="0.22", ranksep="0.5"]; node [shape=box,style="rounded,filled",fillcolor="#edf5f3",color="#8eb5ad",fontname="Noto Sans CJK SC",fontsize=12,margin="0.15,0.10"]; edge [color="#8aa8a2"];']
    for i,label in enumerate(nodes):graph.append(f'n{i} [label={json.dumps(label,ensure_ascii=False)}];')
    graph.extend(f'n{a} -> n{b};' for a,b in edges);graph.append('}')
    if not(args.svg or args.png or args.pdf):raise ValueError('至少选择一种图形输出')
    for fmt in ('svg','png','pdf'):
        target=getattr(args,fmt)
        if not target:continue
        raw=subprocess.run(['dot','-T'+fmt],input='\n'.join(graph).encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True,timeout=60).stdout
        if fmt=='svg':raw=clean_svg(raw.decode()).encode()
        path(target,True).write_bytes(raw)
    if args.json:path(args.json,True).write_text(json.dumps(tree,ensure_ascii=False,indent=2),encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description='报告：Markdown → HTML/PDF；导图：{title,children:[...]} JSON → SVG/PNG/PDF。文件须在 /workspace。')
    commands=parser.add_subparsers(dest='op',required=True)
    p=commands.add_parser('report');p.add_argument('--input',required=True);p.add_argument('--html');p.add_argument('--pdf');p.add_argument('--materials');p.add_argument('--title',default='研究报告')
    p=commands.add_parser('mindmap');p.add_argument('--input',required=True)
    for fmt in ('svg','png','pdf','json'):p.add_argument('--'+fmt)
    p=commands.add_parser('clean-svg');p.add_argument('--input',required=True);p.add_argument('--output',required=True)
    args=parser.parse_args()
    if args.op=='report':report(args)
    elif args.op=='mindmap':mindmap(args)
    else:path(args.output,True).write_text(clean_svg(path(args.input).read_text()),encoding='utf-8')
    print(json.dumps({'ok':True,'operation':args.op}))

if __name__=='__main__':main()
