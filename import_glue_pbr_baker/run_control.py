"""Optional cooperative control shared by the engine and background worker.

A controller implements emit(event: dict) and check_cancel(). The engine calls
it only on Blender's main thread, between bounded Python stages/native calls.
Native Cycles/decoder/export calls are not interruptible through this API.
"""


class RunCancelled(BaseException):
    """User cancellation, never a material failure or a retry/poison strike.

    BaseException deliberately bypasses legacy per-part ``except Exception``
    handlers. Engine.main catches it and returns a distinct cancelled census.
    """

    def __init__(self, message="Cancellation requested"):
        super().__init__(message)
