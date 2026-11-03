"""固定公开来源：校验 DNS 后连接固定 IP，TLS 仍验证原主机；不使用代理、凭据或自动重定向。"""
import asyncio
from html.parser import HTMLParser
import ipaddress
import re
import socket
from urllib.parse import urlencode, urljoin, urlsplit
import xml.etree.ElementTree as ET

import httpx

from app.models.research import ARXIV_ID, ResearchUnavailableError, canonical_url, reference_row
from app.models.chat_tools import clip

MAX_DOWNLOAD=524288


async def public_addresses(host):
    rows=await asyncio.get_running_loop().getaddrinfo(host,443,type=socket.SOCK_STREAM)
    addresses=sorted({row[4][0] for row in rows},key=lambda x:':' in x)
    if not addresses or any(not ipaddress.ip_address(x).is_global for x in addresses):
        raise ResearchUnavailableError('资料网络地址不符合公开网络策略。')
    return addresses


class ArticleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip=0;self.title=False;self.titles=[];self.parts=[];self.main=[];self.in_main=0

    def handle_starttag(self,tag,attrs):
        if tag in {'script','style','nav','header','footer','iframe','svg','noscript'}:self.skip+=1
        if tag in {'main','article'}:self.in_main+=1
        if tag=='title':self.title=True

    def handle_endtag(self,tag):
        if tag in {'script','style','nav','header','footer','iframe','svg','noscript'}:self.skip=max(0,self.skip-1)
        if tag in {'main','article'}:self.in_main=max(0,self.in_main-1)
        if tag=='title':self.title=False

    def handle_data(self,data):
        if self.title:self.titles.append(data)
        elif not self.skip:
            self.parts.append(data)
            if self.in_main:self.main.append(data)

    def result(self):
        return ' '.join(' '.join(self.titles).split()),' '.join(' '.join(self.main or self.parts).split())


class PublicResearchClient:
    def __init__(self,permit,*,resolver=public_addresses,transport=None):
        self.permit,self.resolver,self.transport=permit,resolver,transport

    async def fetch(self,url,*,api=False):
        try:
            async with asyncio.timeout(22):
                for _ in range(3):
                    url=canonical_url(url,api=api)
                    await self.permit()
                    host=urlsplit(url).hostname
                    addresses=await self.resolver(host)
                    # Recheck injected resolver output too; never trust a private first address.
                    if not addresses or any(not ipaddress.ip_address(x).is_global for x in addresses):
                        raise ResearchUnavailableError('地址不符合公开网络策略。')
                    target=httpx.URL(url).copy_with(host=addresses[0])
                    request=httpx.Request('GET',target,headers={'Host':host,'User-Agent':'ExperimentAssistant/0.1 (public-research reader)',
                        'Accept':'application/atom+xml,text/html,application/xhtml+xml','Accept-Encoding':'identity'},
                        extensions={'sni_hostname':host,'timeout':{'connect':6,'read':8,'write':6,'pool':6}})
                    async with httpx.AsyncClient(transport=self.transport,trust_env=False,follow_redirects=False) as client:
                        async with client.stream(request.method,request.url,headers=request.headers,extensions=request.extensions) as response:
                            if response.status_code in {301,302,303,307,308}:
                                url=urljoin(url,response.headers.get('location',''))
                                continue
                            if response.status_code!=200:raise ResearchUnavailableError('公开资料来源未返回可用正文。')
                            if response.headers.get('content-encoding','identity').lower() not in {'','identity'}:
                                raise ResearchUnavailableError('资料压缩格式暂不支持。')
                            length=response.headers.get('content-length')
                            if length and (not length.isdecimal() or int(length)>MAX_DOWNLOAD):
                                raise ResearchUnavailableError('公开资料超过下载预算。')
                            kind=response.headers.get('content-type','').split(';')[0].strip().lower()
                            if kind not in {'text/html','application/xhtml+xml','application/atom+xml','application/xml','text/xml'}:
                                raise ResearchUnavailableError('当前只读取 HTML/Atom，PDF 和其他格式尚未接入。')
                            buf=bytearray()
                            async for chunk in response.aiter_raw():
                                buf.extend(chunk)
                                if len(buf)>MAX_DOWNLOAD:raise ResearchUnavailableError('公开资料超过下载预算。')
                            text=bytes(buf).decode('utf-8')
                            if '\x00' in text:raise ResearchUnavailableError('资料编码暂不支持。')
                            return text,kind,url
                raise ResearchUnavailableError('资料重定向次数超过预算。')
        except ResearchUnavailableError:raise
        except (httpx.HTTPError,OSError,ValueError,UnicodeError,TimeoutError) as error:
            raise ResearchUnavailableError('暂时无法读取公开研究资料。') from error

    async def search(self,query,limit,run_id=None):
        tokens=re.findall(r'[\w-]+',query)[:14]
        if not tokens:return []
        expression=' AND '.join('all:"'+token+'"' for token in tokens)
        url='https://export.arxiv.org/api/query?'+urlencode({'search_query':expression,'start':0,'max_results':limit,'sortBy':'relevance','sortOrder':'descending'})
        text,kind,_=await self.fetch(url,api=True)
        if kind not in {'application/atom+xml','application/xml','text/xml'}:raise ResearchUnavailableError('检索来源没有返回 Atom 数据。')
        if re.search(r'<!\s*(?:DOCTYPE|ENTITY)',text,re.I):raise ResearchUnavailableError('Atom 内容不符合安全解析要求。')
        try:
            root=ET.fromstring(text)
            if root.tag!='{http://www.w3.org/2005/Atom}feed':raise ValueError('not Atom feed')
            rows=[];namespace={'a':'http://www.w3.org/2005/Atom'}
            for entry in root.findall('a:entry',namespace)[:limit]:
                identifier=(entry.findtext('a:id','',namespace).split('/abs/')[-1]).strip()
                if not re.fullmatch(ARXIV_ID,identifier):continue
                title=entry.findtext('a:title','',namespace);summary=entry.findtext('a:summary','',namespace)
                authors=[clip(x.findtext('a:name','',namespace),100) for x in entry.findall('a:author',namespace)[:10]]
                metadata={'provider':'arxiv','authors':authors,'published':entry.findtext('a:published','',namespace)[:40],
                          'query':query,'abstract_only':True}
                row=reference_row(url='https://arxiv.org/abs/'+identifier,title=title,text=summary,kind='search_abstract',metadata=metadata,run_id=run_id)
                rows.append(row)
            return rows
        except (ET.ParseError,ValueError,TypeError) as error:
            raise ResearchUnavailableError('论文检索数据无法解析。') from error

    async def read(self,url,run_id=None):
        text,kind,url=await self.fetch(url)
        if kind not in {'text/html','application/xhtml+xml'}:raise ResearchUnavailableError('资料页面不是 HTML。')
        parser=ArticleText();parser.feed(text);title,body=parser.result()
        if re.search(r'access denied|captcha|just a moment|attention required|page not found|verify you are',title,re.I):
            raise ResearchUnavailableError('资料页面返回了访问限制，未作为论文正文保存。')
        host=urlsplit(url).hostname
        source_kind=('paper_html' if host=='arxiv.org' and '/html/' in url else
                     'paper_abstract' if host in {'arxiv.org','openaccess.thecvf.com'} else 'official_documentation')
        return reference_row(url=url,title=title,text=body,kind=source_kind,run_id=run_id,
                             metadata={'abstract_only':source_kind=='paper_abstract','normalized_text_capped':len(body.encode())>60000})
