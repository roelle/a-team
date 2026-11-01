"""Names from before the rename that existing rosters still use."""


def current_spec(spec: str) -> str:
    """A "module:Class" seam, with a roster's agyteam.x read as ateam.x."""
    return "ateam." + spec[len("agyteam."):] if spec.startswith("agyteam.") else spec
