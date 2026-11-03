"""有界 PDF 子进程与结果验证；单个解析失败不在 API 进程内继续工作。"""
import asyncio
import ctypes
import json
import os
from pathlib import Path
import sys

from app.models.paper import PaperParseError,sha256
from app.services.paper_chunking import MAX_CHARACTERS, OVERLAP_CHARACTERS


class WindowsMemoryJob:
    """上传 PDF 是不可信输入：Windows 开发环境也约束解压/解析内存。"""
    def __init__(self,pid):
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_=[('process_time',ctypes.c_int64),('job_time',ctypes.c_int64),('flags',w.DWORD),
                ('minimum',ctypes.c_size_t),('maximum',ctypes.c_size_t),('processes',w.DWORD),
                ('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class IO(ctypes.Structure):_fields_=[(name,ctypes.c_uint64) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Extended(ctypes.Structure):
            _fields_=[('basic',Basic),('io',IO),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),
                ('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR];kernel.CreateJobObjectW.restype=w.HANDLE
        kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
        kernel.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
        kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
        kernel.CloseHandle.argtypes=[w.HANDLE]
        self.kernel=kernel;self.handle=kernel.CreateJobObjectW(None,None)
        process=None
        try:
            if not self.handle:raise OSError('job unavailable')
            info=Extended();info.basic.flags=0x2000|0x100;info.process_memory=512*1024*1024
            if not kernel.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):raise OSError('memory limit unavailable')
            process=kernel.OpenProcess(0x0100|0x0001,False,pid)
            if not process or not kernel.AssignProcessToJobObject(self.handle,process):raise OSError('job assignment unavailable')
        except BaseException:
            self.close();raise
        finally:
            if process:kernel.CloseHandle(process)

    def close(self):
        if self.handle:self.kernel.CloseHandle(self.handle);self.handle=None


def validate_result(value):
    if type(value) is not dict or value.get('ok') is not True:
        code=value.get('code') if isinstance(value,dict) else None
        raise PaperParseError(code if code in {'INVALID_PDF','ENCRYPTED_PDF','NO_EXTRACTABLE_TEXT','PDF_LIMIT','PARSER_UNAVAILABLE'} else 'PARSER_UNAVAILABLE')
    try:
        data=value['data'];chunks=data['chunks'];count=data['page_count']
        if type(count) is not int or not 1<=count<=200 or type(chunks) is not list or not 1<=len(chunks)<=3000:raise ValueError('limits')
        if type(data['parser_version']) is not str or len(data['parser_version'])>64:raise ValueError('version')
        empty=data['empty_pages']
        if type(empty) is not list or len(empty)!=len(set(empty)) or any(type(n) is not int or not 1<=n<=count for n in empty):raise ValueError('pages')
        previous=None;total=0
        for index,row in enumerate(chunks):
            if set(row)!={'chunk_index','page_number','char_start','content','content_sha256'}:raise ValueError('shape')
            if row['chunk_index']!=index or type(row['page_number']) is not int or not 1<=row['page_number']<=count or row['page_number'] in empty:raise ValueError('position')
            if type(row['char_start']) is not int or row['char_start']<0 or type(row['content']) is not str or not 1<=len(row['content'])<=MAX_CHARACTERS:raise ValueError('text')
            if not row['content'].strip() or sha256(row['content'].encode())!=row['content_sha256']:raise ValueError('digest')
            if previous is None or row['page_number']!=previous['page_number']:
                if row['char_start']!=0 or previous is not None and row['page_number']<previous['page_number']:raise ValueError('page order')
                total+=len(row['content'])
            else:
                overlap=previous['char_start']+len(previous['content'])-row['char_start']
                if not previous['char_start']<row['char_start'] or not 0<=overlap<=OVERLAP_CHARACTERS:raise ValueError('overlap')
                if overlap and row['content'][:overlap]!=previous['content'][-overlap:]:raise ValueError('overlap text')
                total+=len(row['content'])-overlap
            previous=row
        if total>2000000 or set(range(1,count+1))!=set(empty)|{row['page_number'] for row in chunks}:raise ValueError('incomplete pages')
        return data
    except (ValueError,KeyError,TypeError) as error:raise PaperParseError('PARSER_UNAVAILABLE') from error


class PdfParser:
    def __init__(self,timeout=45):self.timeout=timeout

    async def parse(self,raw):
        # Uvicorn --reload 在 Windows 使用 SelectorEventLoop，不能启动子进程。
        # 解析任务在自己的 Proactor 循环运行，不修改整个 API 的事件循环策略。
        if os.name == 'nt':
            task=asyncio.create_task(asyncio.to_thread(self._windows_parse,raw))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # 等有界解析器清理完子进程再释放槽位，避免取消后遗留解析任务。
                try:await task
                except Exception:pass
                raise
        return await self._parse(raw)

    def _windows_parse(self,raw):
        with asyncio.Runner(loop_factory=asyncio.ProactorEventLoop) as runner:
            return runner.run(self._parse(raw))

    async def _parse(self,raw):
        process=None;job=None
        try:
            process=await asyncio.create_subprocess_exec(sys.executable,'-B',str(Path(__file__).with_name('pdf_parser_process.py')),
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,
                **({'creationflags':0x08000000} if os.name=='nt' else {}))
            if os.name=='nt':job=WindowsMemoryJob(process.pid)
            async with asyncio.timeout(self.timeout):
                output,error=await process.communicate(raw)
            if process.returncode!=0 or error or len(output)>8*1024*1024:raise PaperParseError('PDF_LIMIT')
            try:return validate_result(json.loads(output))
            except (ValueError,UnicodeError) as error:raise PaperParseError('PARSER_UNAVAILABLE') from error
        except TimeoutError as error:raise PaperParseError('PDF_TIMEOUT') from error
        except OSError as error:raise PaperParseError('PARSER_UNAVAILABLE') from error
        finally:
            if process is not None:
                if process.returncode is None:
                    try:process.kill()
                    except ProcessLookupError:pass
                await process.wait()
            if job is not None:job.close()
