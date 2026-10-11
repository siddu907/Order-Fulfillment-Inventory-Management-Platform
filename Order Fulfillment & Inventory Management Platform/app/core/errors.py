from fastapi import status


class DomainError(Exception):
    def __init__(self, code: str, message: str, http_status: int = status.HTTP_400_BAD_REQUEST):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)


def not_found(entity: str) -> DomainError:
    return DomainError("NOT_FOUND", f"{entity} not found", status.HTTP_404_NOT_FOUND)


def forbidden(code: str = "FORBIDDEN", message: str = "You are not authorized to access this resource") -> DomainError:
    return DomainError(code, message, status.HTTP_403_FORBIDDEN)
