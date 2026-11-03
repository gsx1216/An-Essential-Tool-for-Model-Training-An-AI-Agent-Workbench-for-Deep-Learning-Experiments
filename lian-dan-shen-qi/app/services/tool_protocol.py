"""阻止正文中的工具协议显示；不从正文解析或执行工具。"""
import re
from app.errors import ChatToolProtocolError

MARKUP = re.compile(r'</?[｜|]+DSML[｜|]+', re.I)
PARTIAL = re.compile(r'(?:</?[｜|]+(?:D(?:S(?:M(?:L)?)?)?)?|</?)$', re.I)

class ToolTextProtocolError(ChatToolProtocolError):
    pass

def contains_tool_markup(text):
    return isinstance(text,str) and MARKUP.search(text) is not None

class ToolTextStream:
    """保留可能的协议前缀，处理跨 chunk 标记；普通文字仍逐块输出。"""
    def __init__(self):
        self.pending=''
        self.blocked=False
    def push(self,text):
        if self.blocked:return ''
        self.pending+=text
        marker=MARKUP.search(self.pending)
        if marker:
            visible=self.pending[:marker.start()]
            self.pending=''
            self.blocked=True
            return visible
        partial=PARTIAL.search(self.pending)
        cut=partial.start() if partial else len(self.pending)
        visible,self.pending=self.pending[:cut],self.pending[cut:]
        return visible
    def finish(self):
        if self.pending.startswith(('<｜','</｜','<|D','</|D','<||D','</||D')):self.blocked=True
        text='' if self.blocked else self.pending
        self.pending=''
        return text
