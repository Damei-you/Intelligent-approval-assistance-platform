class ContractChatError(RuntimeError):
    """可以安全转换为前端响应的合同问答业务异常。"""

    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
