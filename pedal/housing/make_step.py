#!/usr/bin/env python3
"""Same tray as make_base.py as true CAD solids, exported as STEP (opens in Fusion).
  pip install cadquery ; python3 housing/make_step.py"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
import cadquery as cq
import make_base as mb

def _box(x0, y0, z0, x1, y1, z1):
    return cq.Workplane("XY").box(x1-x0, y1-y0, z1-z0, centered=False).translate((x0, y0, z0))

def _slab(x0, y0, x1, y1, z0, z1, r):
    return (cq.Workplane("XY").workplane(offset=z0).center((x0+x1)/2, (y0+y1)/2)
            .rect(x1-x0, y1-y0).extrude(z1-z0).edges("|Z").fillet(r))

def _solid(kind, a):
    if kind == 'box':  return _box(*a)
    if kind == 'slab': return _slab(*a)
    if kind == 'cyl':
        x, y, z0, h, d = a
        return cq.Workplane("XY").workplane(offset=z0).center(x, y).circle(d/2).extrude(h)
    if kind == 'xzprism':
        pts, y0, y1 = a
        return cq.Workplane("XZ").workplane(offset=-y0).polyline(pts).close().extrude(-(y1 - y0))
    if kind == 'ycyl':
        x, y0, z, h, d = a
        return cq.Workplane("XZ").workplane(offset=-y0).center(x, z).circle(d/2).extrude(-h)
    raise ValueError(kind)

ops, ext = mb.geometry()
t = None
for op, kind, a in ops:
    s = _solid(kind, a)
    t = s if t is None else (t.union(s) if op == 'add' else t.cut(s))
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "PureIR-base.step")
cq.exporters.export(t, out)
v = t.val(); bb = v.BoundingBox()
print("valid", v.isValid(), "volume cm3 %.1f" % (v.Volume()/1000), "bbox %.1f %.1f %.1f" % (bb.xlen, bb.ylen, bb.zlen))

lid = None
for op, kind, a in mb.lid_geometry():
    sld = _solid(kind, a)
    lid = sld if lid is None else (lid.union(sld) if op == 'add' else lid.cut(sld))
lout = out.replace("base", "lid")
cq.exporters.export(lid, lout)
lv = lid.val(); print("lid valid", lv.isValid(), "volume cm3 %.1f" % (lv.Volume()/1000))
