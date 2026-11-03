"""所有仓储共用的输入校验；校验失败时抛出 ValueError。"""

from uuid import UUID


def validate_positive_integer(value: int, label: str) -> int:
    # bool 在 Python 中属于 int 的子类，这里也明确拒绝。
    if type(value) is not int or not 1 <= value <= 2**64 - 1:
        raise ValueError(f"{label}必须是大于 0 的整数，且不能超过 BIGINT UNSIGNED 的范围。")
    return value


def normalize_required_text(value: str, label: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是字符串。")
    value = value.strip()
    if not value:
        raise ValueError(f"{label}不能为空。")
    if len(value) > max_length:
        raise ValueError(f"{label}不能超过 {max_length} 个字符。")
    return value


def normalize_session_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("会话 ID 必须是 UUID 字符串。")
    try:
        return UUID(value.strip()).hex
    except ValueError as error:
        raise ValueError("会话 ID 格式不正确，请复制完整的 UUID。") from error


def validate_message_content(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("消息内容必须是非空字符串。")
    # 保留正文的空格和换行，例如代码块的缩进。
    return value
