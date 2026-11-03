"""请求校验错误的全局处理器：不向前端返回密码或原始请求内容。"""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


async def request_validation_exception_handler(
    request: Request,
    error: RequestValidationError,
) -> JSONResponse:
    # TODO 1：创建空列表 safe_errors，用来保存整理后的错误。
    safe_errors = []
    # TODO 2：遍历 error.errors()，每一项用 item 接收。
    for item in error.errors():
    # TODO 3：为每一项创建新的字典 safe_error，只包含：
    # loc：list(item["loc"])，表示出错字段的位置。
    # type：item["type"]，表示错误类型。
    # msg：固定文字“请求字段缺失或格式不正确。”。
    # 不复制 input、ctx、原始请求体或完整错误字典。
        safe_error = {
            "loc": list(item["loc"]),
            "type": item["type"],
            "msg": "请求字段缺失或格式不正确。",
        }
    # TODO 4：把 safe_error 加入 safe_errors。
        safe_errors.append(safe_error)
    # TODO 5：返回 JSONResponse：
    # status_code 为 422，content 为 {"detail": safe_errors}。
    return JSONResponse(
        status_code=422,
        content={"detail": safe_errors},
    )
