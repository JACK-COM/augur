"""A whole ask call as one JSON object, the shape `augur ask --request` reads and Grille's
scorer contract sends.
"""



REQUEST_KEYS = {"questions", "text", "state", "subject", "siblings", "model", "caller"}


def _request(req):
    """A whole `ask` call as one JSON object -> ask()'s arguments. Raises ValueError naming
    the first fault; an unknown key is a fault, so a misspelled `subjcet` is never dropped."""
    if not isinstance(req, dict):
        raise ValueError("the request is not a JSON object")
    unknown = sorted(set(req) - REQUEST_KEYS)
    if unknown:
        raise ValueError(f"unknown request key {unknown[0]!r}; known: {', '.join(sorted(REQUEST_KEYS))}")
    if not isinstance(req.get("questions"), dict) or not req["questions"]:
        raise ValueError("`questions` must be a non-empty object of named questions")
    if len(req["questions"]) > 64:
        raise ValueError("at most 64 questions in one request")
    if ("text" in req) == ("state" in req):
        raise ValueError("give exactly one of `text` (a string) and `state` (an object)")
    if "text" in req and not isinstance(req["text"], str):
        raise ValueError("`text` must be a string")
    if "state" in req and not isinstance(req["state"], dict):
        raise ValueError("`state` must be an object")
    sib = req.get("siblings")
    if sib is not None and not (isinstance(sib, list) and all(isinstance(s, str) for s in sib)):
        raise ValueError("`siblings` must be a list of strings")
    for k in ("subject", "model", "caller"):
        if req.get(k) is not None and not (isinstance(req[k], str) and req[k]):
            raise ValueError(f"`{k}` must be a non-empty string")
    return (req["text"] if "text" in req else req["state"], req["questions"],
            {"subject": req.get("subject"), "siblings": sib, "model": req.get("model"), "caller": req.get("caller")})
