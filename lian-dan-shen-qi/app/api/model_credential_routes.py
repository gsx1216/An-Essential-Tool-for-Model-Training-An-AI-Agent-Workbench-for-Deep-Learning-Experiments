"""个人Key HTTP练习：填写并检查后，再由main挂载。"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from mysql.connector import Error as MySQLError

from app.api.auth_dependencies import require_current_user
from app.api.dependencies import get_model_credential_service
from app.api.schemas import ModelCredentialRequest, ModelCredentialStatusResponse, UserResponse
from app.logging_config import log_failure
from app.services.model_credential_service import ModelCredentialService


router = APIRouter(prefix="/me/model-credentials/deepseek", tags=["个人模型配置"])
logger = logging.getLogger(__name__)
ERROR_RESPONSES = {
    400: {"description": "请求参数不符合要求。"},
    401: {"description": "登录已失效，请重新登录。"},
    503: {"description": "请稍后重试。"},
}


@router.put("", response_class=Response, response_model=None, status_code=204,
            summary="保存或更新自己的DeepSeek Key", responses=ERROR_RESPONSES)
async def save_model_credential(
    request: ModelCredentialRequest,
    current_user: UserResponse = Depends(require_current_user),
    service: ModelCredentialService = Depends(get_model_credential_service),
) -> Response:
    try:
        # TODO 1：request.api_key.get_secret_value()取得原始字符串，用api_key接收。
        api_key = request.api_key.get_secret_value()
        # TODO 2：await service.save_api_key，传user_id=current_user.user_id、api_key=api_key。
        # 业务本身是async，直接await；不再套to_thread，不返回Key。
        await service.save_api_key(user_id=current_user.user_id, api_key=api_key)
    except ValueError as error:
        log_failure(logger, error, event="operation_rejected",
                    operation="model_credential_routes.save", level=logging.WARNING)
        raise HTTPException(status_code=400, detail="请求参数不符合要求。") from error
    except MySQLError as error:
        log_failure(logger, error, event="database_failure", operation="model_credential_routes.save")
        raise HTTPException(status_code=503, detail="请稍后重试。") from error

    # TODO 3：返回空Response(status_code=204)，
    # headers={"Cache-Control": "no-store", "Pragma": "no-cache"}。
    return Response(
        status_code=204,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
    raise NotImplementedError("TODO 3：返回不含Key的204响应。")


@router.get("", response_model=ModelCredentialStatusResponse, status_code=200,
            summary="查看自己的DeepSeek配置状态", responses=ERROR_RESPONSES)
async def read_model_credential_status(
    response: Response,
    current_user: UserResponse = Depends(require_current_user),
    service: ModelCredentialService = Depends(get_model_credential_service),
) -> ModelCredentialStatusResponse:
    try:
        # TODO 4：await service.is_configured(current_user.user_id)，用configured接收。
        # 只查状态，不调用get_api_key，不读取或返回原始Key。
        configured = await service.is_configured(user_id=current_user.user_id)
    except ValueError as error:
        log_failure(logger, error, event="operation_rejected",
                    operation="model_credential_routes.status", level=logging.WARNING)
        raise HTTPException(status_code=400, detail="请求参数不符合要求。") from error
    except MySQLError as error:
        log_failure(logger, error, event="database_failure", operation="model_credential_routes.status")
        raise HTTPException(status_code=503, detail="请稍后重试。") from error

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    # TODO 5：返回ModelCredentialStatusResponse(configured=configured)。
    # provider使用模型的deepseek默认值。
    return ModelCredentialStatusResponse(configured=configured)


@router.delete("", response_class=Response, response_model=None, status_code=204,
               summary="删除自己的DeepSeek配置", responses=ERROR_RESPONSES)
async def delete_model_credential(
    current_user: UserResponse = Depends(require_current_user),
    service: ModelCredentialService = Depends(get_model_credential_service),
) -> Response:
    try:
        # TODO 6：await service.delete_api_key(current_user.user_id)。
        # 已经没有配置时返回False也正常，接口仍返回204；无需if判断失败。
        await service.delete_api_key(current_user.user_id)
    except ValueError as error:
        log_failure(logger, error, event="operation_rejected",
                    operation="model_credential_routes.delete", level=logging.WARNING)
        raise HTTPException(status_code=400, detail="请求参数不符合要求。") from error
    except MySQLError as error:
        log_failure(logger, error, event="database_failure", operation="model_credential_routes.delete")
        raise HTTPException(status_code=503, detail="请稍后重试。") from error

    # TODO 7：返回空Response，status_code=204，
    # headers={"Cache-Control": "no-store", "Pragma": "no-cache"}。
    return Response(
        status_code=204,
    headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
