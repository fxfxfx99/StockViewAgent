"""当前请求所属用户；供深层行情/LLM 服务读取该用户的付费凭证。"""
from contextvars import ContextVar

current_user_id: ContextVar[int | None] = ContextVar("current_user_id", default=None)

