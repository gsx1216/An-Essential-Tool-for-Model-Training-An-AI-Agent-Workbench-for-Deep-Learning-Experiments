"""PDF 子进程入口：只做文本提取，不执行附件、脚本、链接或 OCR。"""
import hashlib
import io
import json
import logging
import os
import re
import sys
import unicodedata

if __package__:
    from .paper_chunking import normalize_page, page_chunks, SPLITTER_VERSION
else:
    from paper_chunking import normalize_page, page_chunks, SPLITTER_VERSION

MAX_FILE=10*1024*1024
MAX_OUTPUT=8*1024*1024


class ParseFailure(ValueError):pass


def extract(raw):
    from pypdf import PdfReader, __version__
    if not 1<=len(raw)<=MAX_FILE or not raw.startswith(b'%PDF-'):raise ParseFailure('INVALID_PDF')
    reader=PdfReader(io.BytesIO(raw),strict=True)
    if reader.is_encrypted:raise ParseFailure('ENCRYPTED_PDF')
    count=len(reader.pages)
    if not 1<=count<=200:raise ParseFailure('PDF_LIMIT')
    chunks=[];empty=[];total=0
    for number,page in enumerate(reader.pages,1):
        text=normalize_page(page.extract_text() or '')
        if len(text)>100000:raise ParseFailure('PDF_LIMIT')
        total+=len(text)
        if total>2000000:raise ParseFailure('PDF_LIMIT')
        if not text:empty.append(number);continue
        for start,end in page_chunks(text):
            piece=text[start:end]
            chunks.append({'chunk_index':len(chunks),'page_number':number,'char_start':start,
                'content':piece,'content_sha256':hashlib.sha256(piece.encode()).hexdigest()})
            if len(chunks)>3000:raise ParseFailure('PDF_LIMIT')
    if not chunks:raise ParseFailure('NO_EXTRACTABLE_TEXT')
    return {'page_count':count,'empty_pages':empty,'parser_version':'pypdf-'+__version__+'/'+SPLITTER_VERSION,'chunks':chunks}


def main():
    logging.disable(logging.CRITICAL)
    try:
        if os.name=='posix':
            import resource
            resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,512*1024*1024))
            resource.setrlimit(resource.RLIMIT_CPU,(35,35))
        value={'ok':True,'data':extract(sys.stdin.buffer.read(MAX_FILE+1))}
        data=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()
        if len(data)>MAX_OUTPUT:raise ParseFailure('PDF_LIMIT')
        sys.stdout.buffer.write(data)
    except ParseFailure as error:
        print(json.dumps({'ok':False,'code':str(error)}))
    except MemoryError:print('{"ok":false,"code":"PDF_LIMIT"}')
    except ImportError:print('{"ok":false,"code":"PARSER_UNAVAILABLE"}')
    except Exception:print('{"ok":false,"code":"INVALID_PDF"}')


if __name__=='__main__':main()
