"""统一日志：可读文本、源码定位、请求关联、敏感字段保护与轮转保存。"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import wraps
import inspect
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
from uuid import uuid4


DEFAULT_LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
_configured = False
_configuration_lock = threading.Lock()
_application_logger = logging.getLogger("app")
_application_logger.propagate = False
_application_logger.addHandler(logging.NullHandler())


@dataclass
class LogContext:
    request_id: str = field(default_factory=lambda: uuid4().hex)
    logged_errors: dict[int, tuple[BaseException, int]] = field(default_factory=dict)


_context: ContextVar[LogContext | None] = ContextVar("application_log_context", default=None)


def _identifier(value, fallback="unknown"):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.<>-]{1,160}", value):
        return value
    return fallback


def _exception_chain(error):
    chain, visited = [], set()
    while error is not None and id(error) not in visited and len(chain) < 8:
        visited.add(id(error))
        chain.append(error)
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    return chain


@dataclass(frozen=True)
class _CodeLocation:
    module: str
    file: str
    function: str
    line: int

    def as_dict(self):
        return {"module": self.module, "file": self.file,
                "function": self.function, "line": self.line}


def _code_location(module, filename, function, line):
    path = Path(filename)
    try:
        filename = path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        filename = path.name
    return _CodeLocation(_identifier(module), filename, _identifier(function),
                         line if type(line) is int and line >= 0 else 0)


def _error_location(error):
    """优先定位根因中最深的业务代码；保留完整安全堆栈供继续追查。"""
    fallback = None
    for cause in reversed(_exception_chain(error)):
        locations = []
        tb = cause.__traceback__
        while tb is not None:
            frame = tb.tb_frame
            locations.append(_code_location(frame.f_globals.get("__name__"),
                                           frame.f_code.co_filename,
                                           frame.f_code.co_name, tb.tb_lineno))
            tb = tb.tb_next
        if locations and fallback is None:
            fallback = locations[-1]
        for location in reversed(locations):
            if location.module.startswith("app.") and location.module != __name__:
                return location
    return fallback


_EVENT_LABELS = {
    "operation_started": "开始执行", "operation_succeeded": "执行成功",
    "operation_rejected": "操作被拒绝", "operation_failed": "执行失败",
    "unhandled_exception": "未处理异常", "database_failure": "数据库操作失败",
    "request_completed": "请求结束", "request_failed": "请求异常结束",
    "application_event": "应用事件",
    "analysis_started": "开始后台分析", "analysis_succeeded": "后台分析已保存",
    "analysis_attention": "后台分析需要处理",
    "suggestion_started": "开始生成有据建议", "suggestion_succeeded": "建议处理结果已保存",
    "suggestion_attention": "建议生成需要处理",
    "model_response_received": "模型请求已返回",
    "code_advice_input_ready": "代码建议输入已准备",
    "model_output_invalid": "模型输出字段校验未通过",
    "cache_hit": "上下文缓存命中", "cache_miss": "上下文缓存未命中",
    "cache_skipped": "上下文过大，已跳过缓存", "cache_bypassed": "缓存异常，已降级到 MySQL",
    "cache_reconciled": "上下文缓存已通过数据库核对",
    "event_delivery_completed": "事件投递步骤结束", "event_message_rejected": "事件消息已隔离",
    "event_task_enqueued": "分析任务已入队并确认消息",
    "context_history_incomplete": "历史消息存在缺口，已标记不完整上下文并继续",
}
_REPLY_REASONS = {
    'EMPTY_MODEL_OUTPUT': '模型请求成功返回，但建议正文为空；检查结束状态、输出额度和思考模式',
    'MODEL_OUTPUT_TOO_LARGE': '模型正文超过 60000 字节的保存上限',
    'INVALID_MODEL_OUTPUT': '模型返回的正文类型不是文本',
}

_MYSQL_REASONS = {
    1045: "数据库认证失败", 1049: "数据库不存在", 1054: "数据库字段不存在",
    1062: "数据违反唯一约束", 1146: "数据库表不存在",
    1205: "数据库锁等待超时", 1213: "数据库事务发生死锁",
    1451: "记录仍被其他数据关联", 1452: "关联数据不存在",
    2003: "无法连接数据库服务", 2006: "数据库连接已断开", 2013: "数据库连接中断",
}
_PAPER_REASONS = {
    'INVALID_PDF': 'PDF 结构无法读取，文件可能损坏或格式不受支持',
    'ENCRYPTED_PDF': 'PDF 已加密，当前不支持密码保护的文件',
    'NO_EXTRACTABLE_TEXT': 'PDF 没有可提取文字，扫描件需先进行 OCR',
    'PDF_LIMIT': 'PDF 解析超过资源限制或子进程异常结束',
    'PDF_TIMEOUT': 'PDF 子进程解析超时',
    'PARSER_UNAVAILABLE': 'PDF 解析器无法启动或返回值不符合协议',
    'PROCESS_INTERRUPTED': 'PDF 解析已中断，可使用原件重试',
}
_ERROR_REASONS = {
    'PaperStorageError': '论文存储或处理失败，请查看异常链定位根因',
    'SkillConfigurationError': 'Skill 配置无法加载或格式不符合要求',
    'SkillStorageError': '个人 Skill 或轮次快照无法核对',
    "AnalysisNotFoundError": "分析任务不存在或不可访问",
    "AnalysisConflictError": "分析已完成、仍在执行或本人尚未启用",
    "AnalysisLeaseLostError": "分析租约或本人授权已失效，旧结果未写回",
    "AnalysisProtocolError": "模型分析格式或实际证据路径校验未通过",
    "AnalysisStorageError": "持久分析内容或来源校验未通过",
    "MonitorNotFoundError": "此运行的后台监控未登记或不属于当前用户",
    "MonitorBusyError": "后台监控仍有有效租约，当前不能重新排队",
    "MonitorLeaseLostError": "监控租约已失效，旧 Worker 已停止推进任务",
    "TrainingNotFoundError": "训练任务或计划不存在，或不属于当前用户和会话",
    "TrainingConflictError": "训练配置、计划版本或同轮执行领取发生冲突",
    "TrainingInstructionRequired": "当前人类原文不是明确的目标训练启动指令，本次未执行",
    "ServerNotFoundError": "服务器配置不存在或不属于当前用户",
    "ServerAddressNotAllowedError": "目标 IP 不在部署允许网段内",
    "ServerCredentialDecryptionError": "服务器凭据密文或归属校验失败",
    "ServerStorageError": "服务器配置持久化异常",
    "RunnerConflictError": "运行已有执行绑定或状态不允许启动",
    "RunnerNotStartedError": "运行尚无执行绑定",
    "RunnerScriptNotAllowedError": "脚本不在部署允许列表",
    "RunnerProtocolError": "远端执行协议或快照校验失败",
    "MetricStorageError": "指标或报告持久化校验失败",
    "MetricSourceChangedError": "指标日志缩短或游标边界变化",
    "MetricSourceUnavailableError": "远端指标来源暂不可确认",
    "MetricSyncConflictError": "指标采集版本并发变化",
    "ReportNotFoundError": "运行报告尚未生成",
    "ServerInUseError": "服务器仍有排队中或运行中的训练，请先停止并同步状态",
    "ServerAuthenticationError": "SSH 用户、私钥、口令或密码认证失败",
    "ServerHostKeyError": "SSH 主机指纹不匹配",
    "ServerConnectionTimeoutError": "SSH 连接或认证超时",
    "ServerConnectionError": "SSH 网络或协议连接失败",
    "ServerBusyError": "SSH 连接并发名额已满",
    "RunNotFoundError": "运行或分页游标不存在或不属于当前用户",
    "RunRequestConflictError": "同一运行请求编号被用于不同实验或参数",
    "RunStorageError": "运行快照或持久化状态异常",
    "ExperimentNotFoundError": "实验或分页游标不存在或不属于当前用户",
    "ExperimentStorageError": "实验持久化状态异常",
    "InvalidTokenError": "登录凭证无效、已过期或已撤销",
    "RequestIdConflictError": "同一请求编号被用于不同问题",
    "UnfinishedTurnError": "会话存在尚未完成的提问",
    "NoPendingReplyError": "会话没有等待回复的提问",
    "ContextTooLargeError": "必要上下文超过输入预算",
    "ContextCompactionError": "历史摘要无效或未减少输入占用",
    "ContextConflictError": "摘要版本或消息状态已变化，本次旧结果未保存",
    "ContextStorageError": "持久化上下文格式或覆盖边界异常",
    "InputCompressionError": "当前输入精简未配置或执行失败",
    "EventNotFoundError": "报告事件不存在或不可访问",
    "EventBusyError": "报告事件仍由有效投递租约处理",
    "EventPayloadError": "事件信封或不可变报告快照校验未通过",
    "InvalidCredentialsError": "用户名或密码校验未通过",
    "UsernameAlreadyExistsError": "用户名已存在",
    "ValueError": "参数或业务校验未通过",
    "NotImplementedError": "功能尚未实现",
    "RuntimeError": "运行状态异常", "TypeError": "参数或返回值类型不符合预期",
    "TimeoutError": "操作超时", "FileNotFoundError": "所需文件不存在",
    "PermissionError": "资源访问被拒绝", "ConnectionError": "连接失败",
}


class SafeJsonFormatter(logging.Formatter):
    """不调用 str(error)，不输出请求体、源码行、局部变量或任意 extra。"""

    def as_dict(self, record):
        context = _context.get()
        data = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).astimezone().isoformat(),
            "level": record.levelname,
            "event": record.msg if isinstance(record.msg, str) and record.msg in _EVENT_LABELS else "application_event",
            "logger": _identifier(record.name),
            "request_id": context.request_id if context else "-",
        }
        source = getattr(record, "source_location", None)
        if not isinstance(source, _CodeLocation):
            source = _code_location(record.name, record.pathname, record.funcName, record.lineno)
        data["source"] = source.as_dict()
        data["thread_name"] = _identifier(record.threadName)
        if type(record.thread) is int:
            data["thread_id"] = record.thread
        if type(record.process) is int:
            data["process_id"] = record.process
        # 不格式化 record.args；业务参数、返回值和任意字符串都不会自动进入日志。
        operation = getattr(record, "operation", None)
        if operation is not None:
            data["operation"] = _identifier(operation)
        method = getattr(record, "method", None)
        if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}:
            data["method"] = method
        route = getattr(record, "route", None)
        if isinstance(route, str):
            data["route"] = route[:300]  # 仅由中间件传入已注册的路由模板。
        status = getattr(record, "status_code", None)
        if type(status) is int and 100 <= status <= 599:
            data["status_code"] = status
        for name in ("gap_count", "missing_message_count"):
            value = getattr(record, name, None)
            if type(value) is int and value >= 0:
                data[name] = value
        # 仅接收固定业务字段，不格式化 record.args，不接受 Token/正文等任意元数据。
        for name in ("task_id", "run_id", "event_id", "session_id"):
            value = getattr(record, name, None)
            if type(value) is str and re.fullmatch(r"(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", value):
                data[name] = value
        for name in ("report_version", "attempts", "tail_seq", "summary_version", "user_id"):
            value = getattr(record, name, None)
            if type(value) is int and 0 <= value <= 2**64 - 1:
                data[name] = value
        message_id = getattr(record, "message_id", None)
        if type(message_id) is str and re.fullmatch(r"[0-9]{1,20}-[0-9]{1,20}", message_id):
            data["message_id"] = message_id
        for name, allowed in {
            "cache_mode": {"reload", "delta", "unchanged"}, "fallback": {"mysql"},
            "delivery_status": {"sent", "pending", "attention"},
        }.items():
            value = getattr(record, name, None)
            if type(value) is str and value in allowed:
                data[name] = value
        code = getattr(record, "analysis_code", None)
        if type(code) is str and code in {"MODEL_UNAVAILABLE", "MODEL_TIMEOUT", "INVALID_MODEL_OUTPUT", "INVALID_SOURCE", "KEY_UNAVAILABLE",
                    "ANALYSIS_INTERRUPTED", "WORKER_CANCELLED", "INTERNAL_ERROR", "STORAGE_ERROR", "ATTEMPTS_EXHAUSTED", "INPUT_TOO_LARGE", "EMPTY_MODEL_OUTPUT", "MODEL_OUTPUT_TOO_LARGE"}:
            data["analysis_code"] = code
        for name in ('content_chars','content_bytes','reasoning_chars','output_tokens','reasoning_tokens','network_lines','log_bytes_sent'):
            value=getattr(record,name,None)
            if type(value) is int and 0<=value<=2**40:data[name]=value
        for name in ('content_blank','log_clipped'):
            value=getattr(record,name,None)
            if type(value) is bool:data[name]=value
        kind=getattr(record,'content_kind',None)
        if type(kind) is str and kind in {'text','blocks','other'}:data['content_kind']=kind
        finish=getattr(record,'finish_reason',None)
        if type(finish) is str and finish in {'stop','length','tool_calls','content_filter','unknown'}:data['finish_reason']=finish
        saved = getattr(record, "state_saved", None)
        fields = getattr(record, "validation_fields", None)
        if type(fields) is list:
            allowed_fields={'inferences','recommendations','limitations','text','evidence_paths','verification','<field>'}
            safe_fields=[]
            for value in fields[:8]:
                if type(value) is not str or len(value)>256 or ':' not in value:continue
                path,category=value.rsplit(':',1)
                if not re.fullmatch('[a-z_]+',category):continue
                if not all(part in allowed_fields or (part.isascii() and part.isdecimal()) for part in path.split('.')):continue
                safe_fields.append(value)
            if safe_fields:data['validation_fields']=','.join(safe_fields)
        if type(saved) is bool:
            data["state_saved"] = saved
        duration = getattr(record, "duration_ms", None)
        if type(duration) in (int, float) and math.isfinite(duration):
            data["duration_ms"] = round(duration, 3)
        error_type = getattr(record, "error_type", None)
        if error_type is not None:
            data["error_type"] = _identifier(error_type)
        errno = getattr(record, "errno", None)
        if type(errno) is int and 0 <= errno <= 65535:
            data["errno"] = errno
        if record.exc_info and record.exc_info[1] is not None:
            errors = []
            for error in _exception_chain(record.exc_info[1]):
                item = {"type": _identifier(type(error).__name__), "frames": []}
                errno = getattr(error, "errno", None)
                if type(errno) is int and 0 <= errno <= 65535:
                    item["errno"] = errno
                tb = error.__traceback__
                while tb is not None and len(item["frames"]) < 40:
                    frame = tb.tb_frame
                    item["frames"].append(_code_location(
                        frame.f_globals.get("__name__"), frame.f_code.co_filename,
                        frame.f_code.co_name, tb.tb_lineno,
                    ).as_dict())
                    tb = tb.tb_next
                errors.append(item)
            data["exceptions"] = errors
            data.setdefault("error_type", errors[0]["type"])
            for error in reversed(errors):
                if "errno" in error:
                    data.setdefault("errno", error["errno"])
                    break
            # 手动 logger.exception(...) 同样定位到根因，无需每处设置 extra。
            if not isinstance(getattr(record, "source_location", None), _CodeLocation):
                location = _error_location(record.exc_info[1])
                if location is not None:
                    data["source"] = location.as_dict()
        if "error_type" in data:
            paper_code=getattr(record,'paper_code',None)
            if type(paper_code) is str and paper_code in _PAPER_REASONS:
                data['paper_code']=paper_code
            diagnostic=getattr(record,'reply_code',None)
            if type(diagnostic) is str and diagnostic in _REPLY_REASONS:data['reply_code']=diagnostic
            data["reason"] = _REPLY_REASONS.get(data.get('reply_code')) or _PAPER_REASONS.get(data.get('paper_code')) or _MYSQL_REASONS.get(data.get("errno")) or _ERROR_REASONS.get(
                data["error_type"], "操作异常，请结合异常类型和调用栈定位",
            )
        return data

    def format(self, record):
        data = self.as_dict(record)
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


class SafeTextFormatter(SafeJsonFormatter):
    """采用参考项目的可读布局，但所有内容仍来自安全字段白名单。"""

    @staticmethod
    def _text(value):
        # 技术字段也转义控制字符，避免伪造日志行；不读取原始消息或参数。
        return json.dumps(str(value), ensure_ascii=False)[1:-1]

    def format(self, record):
        data = self.as_dict(record)
        source = data["source"]
        stamp = data["time"].replace("T", " ").replace("+00:00", " UTC")
        location = (f'{source["module"]}:{source["function"]}:{source["line"]}'
                    f' [{source["file"]}]')
        details = [_EVENT_LABELS[data["event"]]]
        for key in ("operation", "method", "route", "status_code", "duration_ms", "error_type", "errno", "paper_code",
                    "gap_count", "missing_message_count", "task_id", "run_id", "event_id", "report_version", "attempts",
                    "analysis_code", "state_saved", "validation_fields", "session_id", "user_id", "tail_seq", "summary_version",
                    "message_id", "cache_mode", "fallback", "delivery_status", "reply_code",
                    "content_kind", "content_chars", "content_bytes", "content_blank", "reasoning_chars",
                    "finish_reason", "output_tokens", "reasoning_tokens", "network_lines", "log_bytes_sent", "log_clipped"):
            if key in data:
                details.append(f"{key}={self._text(data[key])}")
        line = (f'{stamp} | {data["level"]:<8} | {self._text(location)} | '
                f'pid={data.get("process_id", "-")} {data["thread_name"]} | request_id={data["request_id"]} | '
                + " ".join(details))
        lines = [line]
        if "reason" in data:
            lines.append("  原因：" + data["reason"])
        for index, error in enumerate(data.get("exceptions", []), 1):
            label = f'  异常链[{index}]：{error["type"]}'
            if "errno" in error:
                label += f' errno={error["errno"]}'
            lines.append(label)
            for frame in error["frames"]:
                lines.append(f'    {self._text(frame["file"])}:{frame["line"]}'
                             f' in {frame["module"]}.{frame["function"]}')
        return "\n".join(lines)


def _logging_output_failed():
    # logging 默认的 handleError 会输出原始消息、参数及堆栈，这里只输出固定告警。
    try:
        sys.stderr.write("Application log output failed; check the server log directory.\n")
    except Exception:
        pass


class SafeConsoleHandler(logging.StreamHandler):
    def handleError(self, record):
        _logging_output_failed()


class DailyFileHandler(logging.Handler):
    """每天单个 app.txt；文件自身的跨进程锁避免 API/Worker 多行堆栈互相穿插。"""

    def __init__(self, directory, retention_days=7):
        super().__init__()
        self.directory=Path(directory).resolve()
        self.retention_days=retention_days
        self._cleaned_day=None

    def _cleanup(self,today):
        cutoff=today-timedelta(days=self.retention_days-1)
        for directory in self.directory.iterdir():
            if directory.is_symlink() or not directory.is_dir() or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',directory.name):continue
            try:day=datetime.strptime(directory.name,'%Y-%m-%d').date()
            except ValueError:continue
            if day>=cutoff:continue
            for path in directory.iterdir():
                if path.is_symlink() or not (path.name=='app.txt' or re.fullmatch(r'app-\d+\.log(?:\.[1-9]\d*)?',path.name)):continue
                if path.is_file() and path.resolve().parent==directory.resolve():
                    try:path.unlink()
                    except OSError:pass
            try:directory.rmdir()
            except OSError:pass

    def emit(self,record):
        try:
            today=datetime.fromtimestamp(record.created,timezone.utc).astimezone().date()
            directory=self.directory/today.isoformat()
            directory.mkdir(parents=True,exist_ok=True)
            payload=(self.format(record)+'\n').encode('utf-8')
            with open(directory/'app.txt','a+b',buffering=0) as stream:
                stream.seek(0)
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(),msvcrt.LK_LOCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
                try:
                    stream.seek(0,os.SEEK_END)
                    stream.write(payload)
                finally:
                    stream.seek(0)
                    if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                    else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
            if self._cleaned_day!=today:
                self._cleaned_day=today
                try:self._cleanup(today)
                except OSError:_logging_output_failed()
        except Exception:self.handleError(record)

    def handleError(self,record):
        _logging_output_failed()


def configure_logging(directory=None, *, console=None, force=False, log_format=None):
    global _configured
    with _configuration_lock:
        if _configured and not force:
            return
        # Uvicorn 的启动/异常也进入同一日志，访问请求继续由业务中间件记录。
        server_logger=logging.getLogger('uvicorn.error')
        root_logger=logging.getLogger()
        for handler in root_logger.handlers[:]:
            if getattr(handler,'_app_daily',False):root_logger.removeHandler(handler)
        for handler in server_logger.handlers[:]:
            if getattr(handler,'_app_daily',False):server_logger.removeHandler(handler)
        for handler in _application_logger.handlers[:]:
            _application_logger.removeHandler(handler)
            handler.close()
        log_format = log_format or os.getenv("APP_LOG_FORMAT", "text")
        formatter = SafeJsonFormatter() if log_format == "json" else SafeTextFormatter()
        file_handler = DailyFileHandler(directory or os.getenv("APP_LOG_DIR") or DEFAULT_LOG_DIR)
        file_handler.setFormatter(formatter)
        file_handler._app_daily=True
        _application_logger.addHandler(file_handler)
        server_logger.addHandler(file_handler)
        server_logger.propagate=False
        root_logger.addHandler(file_handler)
        if console is None:
            console = os.getenv("APP_LOG_CONSOLE", "1") != "0"
        if console:
            handler = SafeConsoleHandler()
            handler.setFormatter(formatter)
            handler._app_daily=True
            _application_logger.addHandler(handler)
            root_logger.addHandler(handler)
            server_logger.addHandler(handler)
        level = os.getenv("APP_LOG_LEVEL", "INFO").upper()
        configured_level=level if level in {"DEBUG", "INFO", "WARNING", "ERROR"} else "INFO"
        _application_logger.setLevel(configured_level)
        # 主程序 Worker 的 INFO 属于业务日志；第三方只汇总告警/异常，避免 HTTP 库的重复请求摘要。
        logging.getLogger('__main__').setLevel(configured_level)
        server_logger.setLevel(configured_level)
        root_logger.setLevel(max(logging.WARNING,logging._nameToLevel[configured_level]))
        # 原始 Uvicorn access log 可能含查询参数；由我们的安全请求日志替代。
        logging.getLogger("uvicorn.access").disabled = True
        _configured = True


@contextmanager
def logging_context(*, new=False):
    configure_logging()
    current = _context.get()
    token = None
    if new or current is None:
        current = LogContext()
        token = _context.set(current)
    try:
        yield current
    finally:
        if token is not None:
            _context.reset(token)


def log_failure(logger, error, *, event="operation_failed", operation=None, level=logging.ERROR):
    """同一次请求内，已经记录的异常不再逐层重复记录。"""
    with logging_context() as context:
        chain = _exception_chain(error)
        if any(context.logged_errors.get(id(item), (None, 0))[1] >= level for item in chain):
            return
        if len(context.logged_errors) > 256:
            context.logged_errors.clear()
        for item in chain:
            context.logged_errors[id(item)] = (item, level)
        extra = {"error_type": type(error).__name__}
        location = _error_location(error)
        if location is not None:
            extra["source_location"] = location
        for cause in reversed(chain):
            errno = getattr(cause, "errno", None)
            if type(errno) is int and 0 <= errno <= 65535:
                extra["errno"] = errno
                break
        if operation is not None:
            extra["operation"] = operation
        for cause in chain:
            code=getattr(cause,'code',None)
            if type(cause).__name__=='PaperParseError' and type(code) is str and code in _PAPER_REASONS:
                extra['paper_code']=code
                break
        for cause in chain:
            if type(cause).__name__=='ModelReplyError' and getattr(cause,'code',None) in _REPLY_REASONS:
                extra['reply_code']=cause.code
                break
        logger.log(level, event, extra=extra,
                   exc_info=(type(error), error, error.__traceback__) if level >= logging.ERROR else None)


def logged_operation(operation, *, log_success=True, expected_errors=(ValueError,)):
    """业务入口统一记录结果；不读取、序列化或打印参数与返回值。"""
    def decorate(function):
        name = function.__module__
        logger = logging.getLogger(name if name.startswith("app.") else "app." + name)
        source = _code_location(name, function.__code__.co_filename,
                                function.__qualname__, function.__code__.co_firstlineno)
        extra = {"operation": operation, "source_location": source}

        def failed(error):
            expected = isinstance(error, expected_errors)
            log_failure(logger, error, operation=operation,
                        event="operation_rejected" if expected else "operation_failed",
                        level=logging.WARNING if expected else logging.ERROR)

        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def asynchronous(*args, **kwargs):
                with logging_context():
                    started = time.perf_counter()
                    logger.debug("operation_started", extra=extra)
                    try:
                        result = await function(*args, **kwargs)
                    except Exception as error:
                        failed(error)
                        raise
                    if log_success:
                        logger.info("operation_succeeded", extra={**extra,
                                    "duration_ms": (time.perf_counter() - started) * 1000})
                    return result
            return asynchronous

        @wraps(function)
        def synchronous(*args, **kwargs):
            with logging_context():
                started = time.perf_counter()
                logger.debug("operation_started", extra=extra)
                try:
                    result = function(*args, **kwargs)
                except Exception as error:
                    failed(error)
                    raise
                if log_success:
                    logger.info("operation_succeeded", extra={**extra,
                                "duration_ms": (time.perf_counter() - started) * 1000})
                return result
        return synchronous
    return decorate
