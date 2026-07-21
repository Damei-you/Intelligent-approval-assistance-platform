class RiskReviewError(RuntimeError):
    """可安全返回给前端的风险审查业务错误。"""

    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
