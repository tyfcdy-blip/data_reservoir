"""跨模块安全错误，消息不得包含凭据或源响应原文。"""


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
