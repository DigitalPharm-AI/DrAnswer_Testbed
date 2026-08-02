from __future__ import annotations


class SelectionStateError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class SelectionStateEncryptionError(SelectionStateError):
    pass


class DoseSelectionStateError(SelectionStateError):
    pass


class DoseSelectionStateEncryptionError(
    DoseSelectionStateError,
    SelectionStateEncryptionError,
):
    pass
