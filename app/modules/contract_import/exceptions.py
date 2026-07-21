from __future__ import annotations


class ContractImportError(Exception):
    status_code = 400
    code = "CONTRACT_IMPORT_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnsupportedFileTypeError(ContractImportError):
    status_code = 415
    code = "UNSUPPORTED_FILE_TYPE"


class FileTooLargeError(ContractImportError):
    status_code = 413
    code = "FILE_TOO_LARGE"


class DocumentParseError(ContractImportError):
    status_code = 422
    code = "DOCUMENT_PARSE_ERROR"


class ContractTypeNotFoundError(ContractImportError):
    status_code = 422
    code = "CONTRACT_TYPE_NOT_FOUND"


class PreviewFileMismatchError(ContractImportError):
    status_code = 409
    code = "PREVIEW_FILE_MISMATCH"


class ImportRecordNotFoundError(ContractImportError):
    status_code = 404
    code = "IMPORT_RECORD_NOT_FOUND"
