"""The errors Augur raises: unavailable (no answer, the caller's cue to fall back), no key,
and a batch past its cap.
"""



class AugurUnavailable(RuntimeError):
    pass


class NoKey(AugurUnavailable):
    """An entry names where its key lives and nothing is there."""


class BatchTooLarge(ValueError):
    pass
