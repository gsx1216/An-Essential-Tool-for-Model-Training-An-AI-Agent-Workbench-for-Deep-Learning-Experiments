from app.logging_config import logged_operation

from langchain_deepseek import ChatDeepSeek
from app.errors import ChatToolProtocolError
from app.services.tool_protocol import ToolTextProtocolError,ToolTextStream,contains_tool_markup
import asyncio
import logging
import httpx
from app.config import get_model_trust_env
from getpass import getpass

logger=logging.getLogger(__name__)

class ModelGateway:
    @logged_operation("model_gateway.__init__", log_success=False, expected_errors=())
    def __init__(
        self,
        api_key: str,
        model_name: str = "deepseek-flash",
        *, max_retries: int = 2, timeout_seconds: int = 120, trust_env: bool | None = None,
    ):
        if type(max_retries) is not int or not 0 <= max_retries <= 2:
            raise ValueError("模型重试次数必须为 0～2。")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
            raise ValueError("模型请求超时必须为 1～300 秒。")
        trust_env=get_model_trust_env() if trust_env is None else trust_env
        if type(trust_env) is not bool:
            raise ValueError("模型网络代理开关必须为布尔值。")
        # 两个 SDK 客户端采用相同网络配置；不修改进程全局环境，也不关闭 TLS 校验。
        sync_client=httpx.Client(trust_env=trust_env)
        async_client=httpx.AsyncClient(trust_env=trust_env)
        # 使用传入的配置初始化模型
        self.model = ChatDeepSeek(
            api_key=api_key,
            model=model_name,
            temperature=0,
            max_tokens=4096,
            timeout=timeout_seconds,
            max_retries=max_retries,
            http_client=sync_client,
            http_async_client=async_client,
            )

    @logged_operation("model_gateway.invoke")
    async def invoke(self, messages: list, *, max_tokens: int | None = None, tools: list | None = None, tool_choice: str = 'auto', json_mode: bool = False, on_delta=None, thinking: bool | None = None) -> dict:
        target = self.model
        options = {}
        if thinking is not None:
            if type(thinking) is not bool:raise ValueError('思考模式必须是布尔值。')
            options['extra_body']={'thinking':{'type':'enabled' if thinking else 'disabled'}}
        if type(json_mode) is not bool or (json_mode and tools is not None):
            raise ValueError("JSON 输出模式不能与工具调用混用。")
        if json_mode:
            options['response_format'] = {'type': 'json_object'}
            options['extra_body'] = {'thinking': {'type': 'disabled'}}
        if tools is not None:
            if not tools or tool_choice not in {'auto', 'none'}:
                raise ValueError('工具配置不符合要求。')
            target = self.model.bind_tools(tools, tool_choice=tool_choice, parallel_tool_calls=False)
            # Existing persisted history contains final answers, not provider reasoning traces.
            options['extra_body'] = {'thinking': {'type': 'disabled'}}
        # 精简任务可以降低本次输出上限，普通聊天沿用原配置。
        if max_tokens is not None:
            if type(max_tokens) is not int or not 1 <= max_tokens <= 4096:
                raise ValueError("本次模型输出上限必须为 1～4096 的整数。")
            options['max_tokens']=max_tokens
        if on_delta is not None:
            response=None
            display=ToolTextStream() if tools is not None else None
            async for chunk in target.astream(messages,**options):
                response=chunk if response is None else response+chunk
                if isinstance(chunk.content,str) and chunk.content:
                    visible=display.push(chunk.content) if display is not None else chunk.content
                    if visible:await on_delta(visible)
            if response is None:raise ChatToolProtocolError('模型未返回流式内容。')
            if display is not None:
                visible=display.finish()
                if visible:await on_delta(visible)
        else:
            response = await target.ainvoke(messages, **options)
        content=response.content
        # LangChain 允许纯文本或文本块列表；只归并标准文本块，不将 reasoning 当作最终答案。
        if isinstance(content,list) and content and all(isinstance(v,dict) and v.get('type')=='text' and isinstance(v.get('text'),str) for v in content):
            content=''.join(v['text'] for v in content)
        metadata=getattr(response,'response_metadata',{})
        extra=getattr(response,'additional_kwargs',{})
        finish=metadata.get('finish_reason') if isinstance(metadata,dict) else None
        reasoning=extra.get('reasoning_content') if isinstance(extra,dict) else None
        usage=response.usage_metadata if isinstance(response.usage_metadata,dict) else {}
        output_details=usage.get('output_token_details',{})
        stats={'content_kind':'text' if isinstance(content,str) else 'blocks' if isinstance(content,list) else 'other',
               'content_chars':len(content) if isinstance(content,str) else 0,
               'content_bytes':len(content.encode('utf-8')) if isinstance(content,str) else 0,
               'content_blank':not content.strip() if isinstance(content,str) else True,
               'reasoning_chars':len(reasoning) if isinstance(reasoning,str) else 0,
               'finish_reason':finish if type(finish) is str and finish in {'stop','length','tool_calls','content_filter'} else 'unknown',
               'output_tokens':usage.get('output_tokens'),
               'reasoning_tokens':output_details.get('reasoning') if isinstance(output_details,dict) else None}
        logger.info('model_response_received',extra=stats)
        result = {'content': content, 'usage': response.usage_metadata}
        if tools is not None:
            if response.invalid_tool_calls:
                raise ToolTextProtocolError('模型工具响应格式异常。')
            result['tool_calls'] = response.tool_calls
            if contains_tool_markup(result['content']) or (on_delta is not None and display.blocked):
                if result['tool_calls']:result['content']=''
                else:raise ToolTextProtocolError('模型将工具协议错误地输出为正文。')
        return result

    @logged_operation("model_gateway.aclose")
    async def aclose(self) -> None:
        """关闭这个网关拥有的两个SDK客户端；调用者不再复用已关闭网关。"""
        try:
            await self.model.root_async_client.close()
        except BaseException as async_error:
            # 异步关闭失败或取消时，也尝试关闭同步客户端，并保留原始异常。
            try:
                await asyncio.to_thread(self.model.root_client.close)
            except BaseException as sync_error:
                raise async_error from sync_error
            raise
        else:
            await asyncio.to_thread(self.model.root_client.close)

if __name__ == "__main__":
    api_key = getpass("Enter your DeepSeek API key: ")

    gateway = ModelGateway(api_key=api_key)
    messages = [
        ("system","你是一个乐于助人的助手"),
        ("human","你好请介绍一下自己")
    ]
    response = asyncio.run(gateway.invoke(messages))
    print("模型回复：",response["content"])
    print("--------------------------------------------------------")
    print("Token用量:",response["usage"]["total_tokens"])
