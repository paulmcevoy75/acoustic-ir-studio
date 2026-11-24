#!/usr/bin/env python3
"""Parametric 3D-printable base tray for the Daisy Seed3 Pedal DevKit (Rev4).

  pip install manifold3d trimesh numpy matplotlib cadquery
  python3 housing/make_base.py     # writes housing/PureIR-base.stl
  python3 housing/make_step.py     # writes housing/PureIR-base.step (same part, for Fusion)

Walls run all the way round the board, as high as the MIDI sockets, with a hole for
every connector.  The footswitch wing sits in front of the board: its bottom is OPEN so
the switches are pushed up through their holes from below; two small holes in the
tray's front wall pass the switch wires to the FSW_1 / FSW_2 headers.

Coordinates: board origin = bottom-left corner seen from above, X to the right,
Y toward the BACK (jacks), Z up.  mm.  Edit values marked (CHECK) against your board.
"""
import sys
import manifold3d as m
import trimesh

# ---- board (from the Rev4 files) ------------------------------------------
BOARD_X, BOARD_Y, BOARD_T = 197.0, 100.0, 1.6
HOLES = [(5, 5), (5, 95), (192, 5), (192, 95)]            # 3.5 mm mounting holes
# connectors: (position along the edge, hole width)
# NOTE: the board files give each jack's pin origin, not the middle of its plastic body:
# bodies are ~8.7 mm further along +X than the listed positions (measured on the printed tray).
JACK_SHIFT = 8.72
BACK = [(x + JACK_SHIFT, 20) for x in (12, 37, 62, 87, 155)]  # 4 audio jacks + expression (bodies ~18 wide)
DC = (116.5, 14)                                          # barrel jack (body 10 mm wide)
LEFT_SD = (7.0, 28.0)             # microSD slot, Y range (body Y 9-27)
LEFT_SEED_USB = (29.5, 45.5)      # the Seed's own USB-C, Y range (centre Y ~37.5)
# heights relative to the TOP of the board, from the first print: the SD card sits low, the Seed USB-C sits high
SD_Z = (-BOARD_T - 2.0, 5.5)
SEED_Z = (9.5, 19.5)
LEFT_USBC = (70.0, 16)            # board USB-C (body centre Y ~70)
MIDI = (19.5, 90.5)               # one open slot (Y range) for the three MIDI sockets

# ---- heights above the TOP of the board (CHECK each with calipers) ----------
# measured from the bottom of the board: MIDI sockets 23, 1/4" jacks 18, pots 26, toggles 25, push buttons 9
WALL_ABOVE = 29.0 - (BOARD_T)   # wall top = 6 mm above the MIDI sockets = 29 mm above the board bottom
JACK_H = 18.0 + 1.5 - BOARD_T   # hole top = 1.5 mm above the 18 mm-tall jacks (and the DC jack)
USBC_H = 6.0        # board USB-C

# ---- tray settings ---------------------------------------------------------
CLEAR = 1.2        # gap between board edge and wall (was 0.6, a bit tight)
WALL = 3.0
FLOOR = 3.0
STANDOFF = 6.0     # board underside height above the floor (CHECK)
POST_D = 8.0
HOLE_D = 2.8       # pilot hole: M3 screw cuts its own thread.  Use 4.0 for M3 heat-set inserts.
FOOT_D, FOOT_DEPTH = 12.0, 1.0
CORNER_R = 4.0

# ---- footswitch wing (in front of the board; open bottom, solid front) ------
WING_D = 51.0      # was 46: 5 mm more room behind the switches for their stiff wires
WING_W = 96.0
WING_FRONT = 7.0           # thickness of the wing's front wall
POCKET_W = 34.0            # pocket width per switch: body is ~24 mm across (measured) plus its side terminals
POCKET_FRONT_TO_HOLE = 14.0   # switch hole centre, measured from the pocket's front face
PLATE = 3.0
SW_BELOW = 17.0    # bottom of footswitch to top of its lower nut (measured)
SW_LIFT = 2.0      # keep the switch body this far off the table
WING_H = SW_BELOW + SW_LIFT + PLATE   # top of the wing above the table = 22
FSW_HOLE = 12.7    # M12 bushing (caliper: 12.0 mm); printed holes come out a little small
FSW_X = (BOARD_X / 2 - 25, BOARD_X / 2 + 25)
WIRE_W, WIRE_H = 24.0, 14.0   # wire slots through the tray's front wall (big: the switch wires are stiff)
WIRE_Z0 = 4.5                  # bottom of the slots above the table
POCKET_TOP_W = 24.0            # pocket width at its ceiling: the sides slope in, so the roof spans only this much
POCKET_TAPER_FROM = 12.0       # the slope starts this high above the table (above the 11.5 mm switch body)

def geometry():
    """Return (ops, extents).  ops = list of ('add'|'cut', kind, args)."""
    ix0, iy0, ix1, iy1 = -CLEAR, -CLEAR, BOARD_X + CLEAR, BOARD_Y + CLEAR
    ox0, oy0, ox1, oy1 = ix0 - WALL, iy0 - WALL, ix1 + WALL, iy1 + WALL
    zbt = FLOOR + STANDOFF + BOARD_T          # top of the board
    zt = zbt + WALL_ABOVE                      # top of the walls
    zc = zbt - BOARD_T - 0.5                   # bottom of connector holes
    ops = []
    A = lambda k, *a: ops.append(('add', k, a))
    C = lambda k, *a: ops.append(('cut', k, a))
    A('slab', ox0, oy0, ox1, oy1, 0, zt, CORNER_R + WALL)
    C('slab', ix0, iy0, ix1, iy1, FLOOR, zt + 1, CORNER_R)
    for hx, hy in HOLES:
        A('cyl', hx, hy, FLOOR - 0.01, STANDOFF + 0.01, POST_D)
    for hx, hy in HOLES:
        C('cyl', hx, hy, 1.0, STANDOFF + FLOOR - 1.0, HOLE_D)   # blind, stops 1 mm below the post top
    # back wall: closed holes
    for p, w in BACK:
        C('box', p - w / 2, BOARD_Y - 1, zc, p + w / 2, oy1 + 1, zbt + JACK_H)
    p, w = DC
    C('box', p - w / 2, BOARD_Y - 1, zc, p + w / 2, oy1 + 1, zbt + JACK_H)
    # left wall
    a, b = LEFT_SD
    C('box', ox0 - 1, a, zbt + SD_Z[0], 1, b, zbt + SD_Z[1])
    a, b = LEFT_SEED_USB
    C('box', ox0 - 1, a, zbt + SEED_Z[0], 1, b, zbt + SEED_Z[1])
    p, w = LEFT_USBC
    C('box', ox0 - 1, p - w / 2, zc, 1, p + w / 2, zbt + USBC_H)
    # right wall: one open slot for the three MIDI sockets (wall is as high as the sockets)
    a, b = MIDI
    C('box', BOARD_X - 1, a, zc, ox1 + 1, b, zt + 1)
    # footswitch wing: a solid block (strong) with one pocket per switch, cut up from the bottom.
    # Each pocket is only POCKET_W wide, so its ceiling bridges without supports.
    wx0, wx1 = BOARD_X / 2 - WING_W / 2, BOARD_X / 2 + WING_W / 2
    wy0, wy1 = oy0 - WING_D, oy0 + 1.0
    A('slab', wx0, wy0, wx1, wy1, 0, WING_H, 3.0)
    fy = wy0 + WING_FRONT + POCKET_FRONT_TO_HOLE
    for fx in FSW_X:
        hw, tw, zr = POCKET_W / 2, POCKET_TOP_W / 2, WING_H - PLATE
        C('xzprism', [(-hw + fx, -1), (hw + fx, -1), (hw + fx, POCKET_TAPER_FROM), (tw + fx, zr), (-tw + fx, zr), (-hw + fx, POCKET_TAPER_FROM)], wy0 + WING_FRONT, oy0)
        C('cyl', fx, fy, WING_H - PLATE - 1, PLATE + 2, FSW_HOLE)
        # slot through the tray's front wall: wire + plug go on inside the tray to the header
        C('box', fx - WIRE_W / 2, oy0 - 1, WIRE_Z0, fx + WIRE_W / 2, iy0 + 1, WIRE_Z0 + WIRE_H)
    for fx, fy2 in [(wx0 + 12, wy0 + 12), (wx1 - 12, wy0 + 12)]:
        pass
    for fx, fy2 in [(14, 14), (14, 86), (183, 14), (183, 86), (wx0 + 8, wy0 + 8), (wx1 - 8, wy0 + 8)]:
        C('cyl', fx, fy2, -0.01, FOOT_DEPTH, FOOT_D)
    return ops, (ox0, wy0, ox1, oy1, zt)

# ---- partial friction-fit lid -------------------------------------------------
LID_T = 3.0        # plate thickness
LID_SKIRT = 3.0    # skirt wall thickness (wraps the OUTSIDE of the tray wall)
LID_DEPTH = 7.0    # skirt depth below the wall top (stays above the jack holes)
LID_GAP = 0.35     # fit clearance per side (loosened for a first print)
# Windows in the lid (X0, Y0, X1, Y1, corner radius), in board coordinates.  Generous on purpose.
LID_WINDOWS = [
    (104.0, 20.0, 174.0, 68.0, 5.0),   # the six pots (shafts at X 120/138/156, Y 30/54; knobs are ~12 mm across)
    (20.0, 3.0, 78.0, 30.0, 5.0),      # the three toggle switches and LED2
]
LID_LED_HOLES = [(156.0, 9.0, 14.0)]    # LED1 (x, y, diameter)

def lid_geometry():
    """Full-perimeter lid, in tray coordinates with the plate on top (print it upside down)."""
    ix0, iy0, ix1, iy1 = -CLEAR, -CLEAR, BOARD_X + CLEAR, BOARD_Y + CLEAR
    ox0, oy0, ox1, oy1 = ix0 - WALL, iy0 - WALL, ix1 + WALL, iy1 + WALL
    zt = FLOOR + STANDOFF + BOARD_T + WALL_ABOVE
    g, t = LID_GAP, LID_SKIRT
    r = CORNER_R + WALL
    ops = []
    ops.append(('add', 'slab', (ox0 - g - t, oy0 - g - t, ox1 + g + t, oy1 + g + t, zt - LID_DEPTH, zt + LID_T, r + g + t)))
    ops.append(('cut', 'slab', (ox0 - g, oy0 - g, ox1 + g, oy1 + g, zt - LID_DEPTH - 1, zt, r + g)))
    # no skirt along the open MIDI slot (the sockets stick out there)
    ops.append(('cut', 'box', (ox1 - 1, MIDI[0], zt - LID_DEPTH - 1, ox1 + g + t + 1, MIDI[1], zt)))
    for x0, y0, x1, y1, rr in LID_WINDOWS:
        ops.append(('cut', 'slab', (x0, y0, x1, y1, zt - 1, zt + LID_T + 1, rr)))
    for x, y, d in LID_LED_HOLES:
        ops.append(('cut', 'cyl', (x, y, zt - 1, LID_T + 2, d)))
    return ops

# ---- manifold3d backend ------------------------------------------------------
def _box(x0, y0, z0, x1, y1, z1):
    return m.Manifold.cube((x1 - x0, y1 - y0, z1 - z0)).translate((x0, y0, z0))

def _slab(x0, y0, x1, y1, z0, z1, r):
    core = _box(x0 + r, y0, z0, x1 - r, y1, z1) + _box(x0, y0 + r, z0, x1, y1 - r, z1)
    for cx, cy in [(x0 + r, y0 + r), (x1 - r, y0 + r), (x0 + r, y1 - r), (x1 - r, y1 - r)]:
        core += m.Manifold.cylinder(z1 - z0, r, r, 48).translate((cx, cy, z0))
    return core

def _solid(kind, a):
    if kind == 'box':
        return _box(*a)
    if kind == 'slab':
        return _slab(*a)
    if kind == 'cyl':
        x, y, z0, h, d = a
        return m.Manifold.cylinder(h, d / 2, d / 2, 48).translate((x, y, z0))
    if kind == 'xzprism':   # polygon in (X, Z) extruded along +Y from y0 to y1
        pts, y0, y1 = a
        sol = m.Manifold.extrude(m.CrossSection([[(float(x), float(z)) for x, z in pts]]), float(y1 - y0))
        return sol.rotate((90, 0, 0)).translate((0, y1, 0))
    if kind == 'ycyl':   # cylinder along +Y starting at y0
        x, y0, z, h, d = a
        c = m.Manifold.cylinder(h, d / 2, d / 2, 48).rotate((-90, 0, 0))
        return c.translate((x, y0, z))
    raise ValueError(kind)

def build_lid():
    body = None
    for op, kind, a in lid_geometry():
        sld = _solid(kind, a)
        body = sld if body is None else (body + sld if op == 'add' else body - sld)
    return body

def build():
    ops, ext = geometry()
    tray = None
    # all 'add' solids first (outer shell etc. are listed first), cuts apply after the shell exists
    for op, kind, a in ops:
        s = _solid(kind, a)
        if tray is None:
            tray = s
        elif op == 'add':
            tray = tray + s
        else:
            tray = tray - s
    return tray, ext

if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "housing/PureIR-base.stl"
    tray, (ox0, wy0, ox1, oy1, zt) = build()
    mesh = tray.to_mesh()
    tm = trimesh.Trimesh(vertices=mesh.vert_properties[:, :3], faces=mesh.tri_verts)
    tm.export(out)
    lid = build_lid().to_mesh()
    lm = trimesh.Trimesh(vertices=lid.vert_properties[:, :3], faces=lid.tri_verts)
    lm.export(out.replace("base", "lid"))
    print(f"lid: watertight={lm.is_watertight}, bounds={lm.bounds.round(1).tolist()}")
    print(f"wrote {out}: {ox1-ox0:.1f} x {oy1-wy0:.1f} x {zt:.1f} mm, "
          f"watertight={tm.is_watertight}, volume={tm.volume/1000:.1f} cm3")
