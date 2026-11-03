"""从实际路由导出 OpenAPI 和接口索引，不连接数据库、SSH 或模型。"""
import argparse
import json
from pathlib import Path


METHODS={'get','post','put','patch','delete','head','options'}


def export(directory):
    from app.main import app
    schema=app.openapi()
    rows=[]
    for path,entry in schema['paths'].items():
        for method,operation in entry.items():
            if method not in METHODS:continue
            rows.append((operation.get('tags',['其他'])[0],method.upper(),path,
                operation.get('summary','').replace('|','／').replace('\n',' '),
                'Bearer Token' if operation.get('security',schema.get('security')) else '否'))
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    text=json.dumps(schema,ensure_ascii=False,indent=2,sort_keys=True)+'\n'
    (directory/'openapi.json').write_text(text,encoding='utf-8')
    markdown='''# 当前接口索引（从代码生成）

生成命令：`python -m scripts.export_api_docs`。此文件及 openapi.json 应随接口变更重新生成。

字段、必填项、默认值、范围及已声明的响应模型见 [OpenAPI 定义](openapi.json)，运行中的 `/docs` 可直接联调。
部分现有接口的响应 Schema 仍较宽泛，前端接入细节应同时阅读 [接口总览与前端约定](../API接口总览与前端接入约定.md)。
此索引中的鉴权标记来自路由声明，不替代对运行时归属校验和错误码的测试。

共 %s 条路径，%s 个 HTTP 操作。不包含 FastAPI 自身的文档页面路由。

| 模块 | 方法 | 路径 | 用途 | 鉴权 |
| --- | --- | --- | --- | --- |
%s
''' % (len(schema['paths']),len(rows),'\n'.join('| %s | %s | `%s` | %s | %s |'%row for row in sorted(rows)))
    (directory/'接口索引.md').write_text(markdown,encoding='utf-8')
    return {'paths':len(schema['paths']),'operations':len(rows),'directory':str(directory.resolve())}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parents[1]/'docs/api')
    args=parser.parse_args()
    print(json.dumps(export(args.output),ensure_ascii=False))


if __name__=='__main__':main()
