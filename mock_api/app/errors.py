"""One error shape for the whole API.

Every failure leaves the same JSON body and the same `error_code` in the log,
so the logs stay parseable without regex archaeology.
"""


class ApiError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details


class Unauthorized(ApiError):
    status_code = 401
    code = "missing_api_key"


class Forbidden(ApiError):
    status_code = 403
    code = "invalid_api_key"


class NotFound(ApiError):
    status_code = 404
    code = "not_found"


class Conflict(ApiError):
    status_code = 409
    code = "conflict"


class DuplicateEmail(Conflict):
    code = "duplicate_email"


class InsufficientStock(Conflict):
    code = "insufficient_stock"


class InvalidTransition(Conflict):
    code = "invalid_status_transition"


class PaymentDeclined(ApiError):
    status_code = 402
    code = "payment_declined"


class RateLimited(ApiError):
    status_code = 429
    code = "rate_limit_exceeded"

    def __init__(self, message: str, retry_after: int = 60, **details):
        super().__init__(message, **details)
        self.retry_after = retry_after
