"""Validate optional stage tracks and reuse the action pack's portable sampler."""

import math

from tools.annalise_stage_track import pixel_offset


def validated_stage_actions(pack, model_name, motion_files, durations):
    if (not isinstance(pack, dict) or pack.get("Version") != 1
            or pack.get("Model") != model_name
            or pack.get("Units") != "neutral_character_visible_height"
            or not isinstance(pack.get("Actions"), list)):
        return {}
    result = {}
    duplicates = set()
    for action in pack["Actions"]:
        try:
            motion = (action["Group"], action["Index"])
            if not isinstance(motion[0], str) or type(motion[1]) is not int:
                continue
            if motion in result or motion in duplicates:
                result.pop(motion, None)
                duplicates.add(motion)
                continue
            duration = float(action["Duration"])
            if (not math.isfinite(duration) or duration <= 0
                    or abs(duration - durations.get(motion, -1)) > .01
                    or action["Motion"] != motion_files.get(motion)):
                continue
            stage = action["Stage"]
            points = stage["OffsetX"]
            flights = stage["Flights"]
            if (not isinstance(points, list) or not points or not isinstance(flights, list)
                    or stage.get("Interpolation", "smoothstep") != "smoothstep"):
                continue
            previous = -1.0
            for t, x in points:
                if (not all(type(v) in (int, float) and math.isfinite(v) for v in (t, x))
                        or not 0 <= t <= duration or t <= previous or abs(x) > .25):
                    raise ValueError("Invalid horizontal stage point")
                previous = t
            previous_end = 0
            for flight in flights:
                start, end, height = (flight[key] for key in ("Start", "End", "Height"))
                if (not all(type(v) in (int, float) and math.isfinite(v)
                            for v in (start, end, height))
                        or not previous_end <= start < end <= duration or not 0 <= height <= .25):
                    raise ValueError("Invalid stage flight")
                previous_end = end
            if (points[0][0] != 0 or points[0][1] != 0 or points[-1][1] != 0):
                continue
            result[motion] = {"duration": duration, "stage": stage}
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return result


def bounded_offset(offset, bbox, bounds):
    """Limit new movement, without relocating an already partially offscreen pet."""
    dx, dy = offset
    if bbox is None or bounds is None:
        return round(dx), round(dy)
    left, top, right, bottom = bbox
    x0, y0, x1, y1 = bounds
    return (
        round(max(min(0, x0 - left), min(max(0, x1 - right), dx))),
        round(max(min(0, y0 - top), min(max(0, y1 - bottom), dy))),
    )
