"""Error type mapped 1:1 to the JSON error body {code, message, detail}."""


class PipelineError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail or {}

    def body(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


def parse_failed(fmt: str, **detail) -> PipelineError:
    return PipelineError(422, "parse_failed", "Data could not be read.", {"format": fmt, **detail})


def too_large(**detail) -> PipelineError:
    return PipelineError(413, "too_large", "Dataset too large to download.", detail)


def upstream_timeout(**detail) -> PipelineError:
    return PipelineError(504, "upstream_timeout", "Data provider timed out. Try again.", detail)
