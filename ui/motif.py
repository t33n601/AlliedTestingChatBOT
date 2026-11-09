"""Flat logo motif: bits on red, meshed steel gears and a hammer tapping the small one.
Shared by chat_ui.py (Gradio) and ragserver.py (/motif.svg for the web UI)."""
import math
import random

GEAR_MODULE = 12           # tooth size shared by all gears, so they can mesh
HAMMER_PIVOT = (27, 328)   # bottom of the handle; the swing rotates around it
IMPACT = (214, 202)        # head corner meeting the small gear at the bottom of the swing
GEAR_DEPTH = (-4, 5)       # flat "extrusion" offset that gives the gears their dark side
FACE, SIDE, LINE = "#979688", "#3d3d3f", "#2f2f31"   # page palette: steel, charcoal, page ink


def gear_path(cx, cy, teeth):
    """Spur gear with trapezoid teeth; tooth i is centred at (i + 0.3) * step."""
    rp = GEAR_MODULE * teeth / 2
    r_out, r_root = rp + GEAR_MODULE * 0.6, rp - GEAR_MODULE * 0.6
    pts, step = [], 2 * math.pi / teeth
    for i in range(teeth):
        a = i * step
        for da, r in ((0.0, r_root), (0.18, r_out), (0.42, r_out), (0.6, r_root)):
            ang = a + da * step
            pts.append(f"{cx + r * math.cos(ang):.1f},{cy + r * math.sin(ang):.1f}")
    return "M" + " L".join(pts) + "Z"


def spinning(cx, cy, teeth, phase, ccw, body, shift=(0, 0)):
    """Wrap `body` so it turns around (cx, cy); a shifted copy turns in lockstep."""
    spin = "reverse" if ccw else "normal"
    return (f'<g transform="translate({shift[0]} {shift[1]})">'
            f'<g class="kb-gear" style="transform-origin:{cx}px {cy}px;'
            f'animation-duration:{teeth * 2}s;animation-direction:{spin}">'
            f'<g transform="rotate({phase:.1f} {cx} {cy})">{body}</g></g></g>')


def gear_train(x, y, first_teeth, chain):
    """Lay out meshed gears. chain = [(teeth, direction_deg), ...]: each next gear sits
    at the given angle from the previous one, exactly a pitch-radius sum away, and is
    phased so one gear's tooth lands in the other's gap on the line between them.
    Drawn in passes (white outline, dark side, face) so neighbours never cover a face."""
    gears = [(x, y, first_teeth, 0.0)]
    for teeth, ang in chain:
        px, py, pt, pphase = gears[-1]
        dist = GEAR_MODULE * (pt + teeth) / 2
        nx = px + dist * math.cos(math.radians(ang))
        ny = py + dist * math.sin(math.radians(ang))
        frac = ((ang - pphase) / (360 / pt)) % 1         # where on its tooth cycle the previous gear is
        phase = (ang + 180) - (frac + 0.5) * (360 / teeth)  # put the opposite feature there
        gears.append((round(nx, 1), round(ny, 1), teeth, phase % 360))

    outline, sides, faces = [], [], []
    for i, (cx, cy, t, ph) in enumerate(gears):
        d = gear_path(cx, cy, t)
        hub = GEAR_MODULE * t / 2 * 0.42
        ccw = i % 2 == 1
        halo = f'<path d="{d}" fill="{LINE}" stroke="{LINE}" stroke-width="4" stroke-linejoin="round"/>'
        outline.append(spinning(cx, cy, t, ph, ccw, halo, GEAR_DEPTH))
        outline.append(spinning(cx, cy, t, ph, ccw, halo))
        for k in (0.5, 1):
            sides.append(spinning(cx, cy, t, ph, ccw, f'<path d="{d}" fill="{SIDE}"/>',
                                  (GEAR_DEPTH[0] * k, GEAR_DEPTH[1] * k)))
        faces.append(spinning(cx, cy, t, ph, ccw,
            f'<path d="{d}" fill="{FACE}"/>'
            f'<circle cx="{cx}" cy="{cy}" r="{hub:.1f}" fill="#4b4b4d"/>'
            f'<circle cx="{cx}" cy="{cy}" r="{hub * 0.45:.1f}" fill="{FACE}"/>'))
    return "".join(outline + sides + faces)


def hammer():
    """Sledge like the logo's: a flat striking face (the visible end, towards the gear),
    a dark front side, a lighter top, and a big rounded shoulder at the back."""
    dx, dy = 8, -24                        # depth of the head: back and up, towards the strike end
    r = 30                                 # radius of the rounded back shoulder
    x0, x1, yt, yb = 84, 216, 112, 158     # front face box; x1 is the striking end
    front = f"M{x0},{yb} V{yt + r} Q{x0},{yt} {x0 + r},{yt} H{x1} V{yb}Z"
    top = (f"M{x0},{yt + r} Q{x0},{yt} {x0 + r},{yt} H{x1} "
           f"L{x1 + dx},{yt + dy} H{x0 + r + dx} Q{x0 + dx},{yt + dy} {x0 + dx},{yt + r + dy}Z")
    face = f"M{x1},{yt} L{x1 + dx},{yt + dy} V{yb + dy} L{x1},{yb}Z"
    shoulder = f"M{x0},{yt + r} Q{x0},{yt} {x0 + r},{yt}"
    handle = '<rect x="139" y="156" width="24" height="230" rx="5"'
    shapes = [f'<path d="{d}"' for d in (top, face, front)] + [handle]
    halo = "".join(f'{sh} fill="{LINE}" stroke="{LINE}" stroke-width="4" stroke-linejoin="round"/>'
                   for sh in shapes)
    return f"""
      <g class="kb-hammer" style="transform-origin:{HAMMER_PIVOT[0]}px {HAMMER_PIVOT[1]}px">
        <g transform="rotate(38 150 170)">
          {halo}
          {handle} fill="#7d7c70"/>
          <rect x="144" y="166" width="5" height="210" rx="2.5" fill="#979688"/>
          <path d="{top}" fill="#6f6e63"/>
          <path d="{face}" fill="#5f5e55"/>
          <path d="{front}" fill="#4b4b4d"/>
          <path d="M{x0 + r},{yt} H{x1} V{yb}" fill="none" stroke="#979688" stroke-width="1.5"/>
          <path d="{shoulder}" fill="none" stroke="#979688" stroke-width="2.5" stroke-linecap="round"/>
        </g>
      </g>"""


def sparks(rnd, n=16):
    """Flat impact: short burst lines plus square 'pixel' sparks thrown up and away
    from the gear, timed to the hammer's strike."""
    x, y = IMPACT
    out = []
    for k in range(7):                       # burst lines around the contact point
        ang = math.radians(-165 + k * 27)
        x1, y1 = x + 9 * math.cos(ang), y + 9 * math.sin(ang)
        x2, y2 = x + 27 * math.cos(ang), y + 27 * math.sin(ang)
        out.append(f'<line class="kb-burst" x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                   f'stroke="#ececea" stroke-width="3" stroke-linecap="square" '
                   f'style="transform-origin:{x}px {y}px"/>')
    for _ in range(n):                       # pixel sparks
        ang = math.radians(rnd.uniform(-170, 10))
        dist = rnd.uniform(40, 105)
        size = rnd.choice([4, 5, 5, 6])
        col = rnd.choice(["#ececea", "#ececea", "#f2d3ae"])
        out.append(f'<rect class="kb-spark" x="{x - size / 2}" y="{y - size / 2}" width="{size}" '
                   f'height="{size}" fill="{col}" '
                   f'style="--dx:{dist * math.cos(ang):.0f}px;--dy:{dist * math.sin(ang):.0f}px"/>')
    return "".join(out)


def motif_svg(width=420, height=300, seed=2000):
    """The right half of the logo, flat and in the page palette: faint mono bits on the
    accent red, steel gears with a charcoal side, a box-headed hammer tapping the small gear."""
    rnd = random.Random(seed)
    rows = "".join(
        f'<text x="-6" y="{y}">{"".join(rnd.choice("01") for _ in range(34))}</text>'
        for y in range(22, height + 22, 24))
    return f"""<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">
      <rect width="{width}" height="{height}" fill="#c14031"/>
      <g class="kb-bits" fill="#fff" fill-opacity="0.32">{rows}</g>
      {gear_train(318, 72, 12, [(10, 75), (8, 155)])}
      {hammer()}
      {sparks(rnd)}
    </svg>"""
