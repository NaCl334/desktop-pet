"""Sample action-pack offsets without depending on the desktop pet or Live2D."""


def evaluate_stage(stage, seconds):
    """Return (screen-right, up) offsets as fractions of neutral character height."""
    x = stage["OffsetX"][0][1]
    for (t0, v0), (t1, v1) in zip(stage["OffsetX"], stage["OffsetX"][1:]):
        if seconds >= t1:
            x = v1
            continue
        if seconds >= t0:
            u = (seconds - t0) / (t1 - t0)
            x = v0 + (v1 - v0) * u * u * (3 - 2 * u)
        break
    y = 0
    for flight in stage["Flights"]:
        if flight["Start"] <= seconds <= flight["End"]:
            u = (seconds - flight["Start"]) / (flight["End"] - flight["Start"])
            y += flight["Height"] * 4 * u * (1 - u)
    return x, y


def pixel_offset(stage, seconds, neutral_visible_height):
    """Return a screen-space offset; positive pixel Y points down."""
    if neutral_visible_height <= 0:
        raise ValueError("neutral_visible_height must be positive")
    x, y = evaluate_stage(stage, seconds)
    return x * neutral_visible_height, -y * neutral_visible_height
