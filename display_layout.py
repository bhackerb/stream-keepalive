"""Deterministic stream layout planning; coordinates are desktop pixels."""

CAPACITIES = {"tv": 4, "widescreen": 6, "vertical": 4}


def plan_layout(displays: dict, display: str, mode: str, stream_ids: list[str]) -> list[dict]:
    if mode not in ("grid", "side-by-side", "focus"):
        raise ValueError("mode must be grid, side-by-side, or focus")
    names = ["widescreen", "vertical"] if display == "dock" else [display]
    if len(set(stream_ids)) != len(stream_ids):
        raise ValueError("stream_ids must be unique")
    regions = []
    for name in names:
        if name not in CAPACITIES or name not in displays:
            raise ValueError(f"Display {name!r} is not configured")
        region = displays[name]
        if not isinstance(region, dict) or any(type(region.get(k)) is not int for k in ("x", "y", "width", "height")):
            raise ValueError(f"Display {name!r} requires integer x/y/width/height")
        if region["width"] <= 0 or region["height"] <= 0:
            raise ValueError("Display dimensions must be positive")
        capacity = CAPACITIES[name] if mode == "grid" else (2 if mode == "side-by-side" else 1)
        if name == "vertical" and mode == "side-by-side":
            raise ValueError("Side-by-side is supported on TV and widescreen")
        regions.append((name, region, capacity))
    if len(stream_ids) > sum(r[2] for r in regions):
        raise ValueError("Too many selected streams for this layout")
    result = []
    remaining = list(stream_ids)
    for name, region, capacity in regions:
        selected, remaining = remaining[:capacity], remaining[capacity:]
        n = len(selected)
        columns = 1 if name == "vertical" or n <= 1 else (3 if n > 4 else 2)
        rows = (n + columns - 1) // columns
        for i, sid in enumerate(selected):
            row, col = divmod(i, columns)
            # Fill a partial final row evenly as well; no unused half-cell.
            row_columns = min(columns, n - row * columns)
            x0 = region["width"] * col // row_columns
            x1 = region["width"] * (col + 1) // row_columns
            # Row height follows its stream count, keeping partial-row areas equal.
            y0 = region["height"] * (row * columns) // n
            y1 = region["height"] * (row * columns + row_columns) // n
            result.append({"stream_id": sid, "display": name, "x": region["x"] + x0,
                           "y": region["y"] + y0, "width": x1 - x0, "height": y1 - y0})
    return result
