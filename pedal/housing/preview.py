#!/usr/bin/env python3
"""Draw housing/PureIR-base-preview.png: top view with the board overlaid."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
import numpy as np, trimesh, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
import make_base as mb

tray, _ = mb.build(); me = tray.to_mesh()
tm = trimesh.Trimesh(me.vert_properties[:, :3], me.tri_verts)
b = tm.bounds
fig = plt.figure(figsize=(14, 6.5))
ax = fig.add_subplot(1, 1, 1); ax.set_aspect('equal')
ax.add_patch(Rectangle((b[0][0], b[0][1]), b[1][0]-b[0][0], b[1][1]-b[0][1], fc='#d9e3f0', ec='k'))
ax.add_patch(Rectangle((-mb.CLEAR, -mb.CLEAR), 197+2*mb.CLEAR, 100+2*mb.CLEAR, fc='white', ec='gray'))
ax.add_patch(Rectangle((0, 0), 197, 100, fc='#1d6b3a', alpha=.35, ec='#0a3'))
for hx, hy in mb.HOLES:
    ax.add_patch(Circle((hx, hy), mb.POST_D/2, fc='#888', ec='k')); ax.add_patch(Circle((hx, hy), mb.HOLE_D/2, fc='w', ec='k'))
for pos, w in mb.BACK+[mb.DC]:  ax.add_patch(Rectangle((pos-w/2, 98), w, 10, fc='orange', alpha=.5))
for ya, yb in [mb.LEFT_SD, mb.LEFT_SEED_USB, (mb.LEFT_USBC[0]-mb.LEFT_USBC[1]/2, mb.LEFT_USBC[0]+mb.LEFT_USBC[1]/2)]: ax.add_patch(Rectangle((-8, ya), 10, yb-ya, fc='orange', alpha=.5))
ax.add_patch(Rectangle((195, mb.MIDI[0]), 10, mb.MIDI[1]-mb.MIDI[0], fc='orange', alpha=.5))
for x in mb.FSW_X: ax.add_patch(Rectangle((x-mb.WIRE_W/2, -4.2), mb.WIRE_W, 3.0, fc='blue', alpha=.6)); ax.add_patch(Rectangle((x-mb.POCKET_W/2, -4.2-mb.WING_D+mb.WING_FRONT), mb.POCKET_W, mb.WING_D-mb.WING_FRONT, fc='none', ec='red', ls='--'))
parts = {'FSW1':(120,10),'FSW2':(138,10),'K1':(120,54),'K2':(138,54),'K3':(156,54),'K4':(120,30),'K5':(138,30),'K6':(156,30),
         'SW1':(30,10),'SW2':(48,10),'SW3':(66,10),'LED1':(156,9),'LED2':(30,23),'USB-C':(6,72.1),'SD':(8.3,18.3),'Seed':(2.8,29.7),
         'IN L':(87,83),'IN R':(62,83),'OUT L':(37,83),'OUT R':(12,83),'DC':(116.5,86),'EXP':(155,83)}
for n, (x, y) in parts.items():
    ax.plot(x, y, 'k.'); ax.text(x+1, y+1, n, fontsize=7)
ax.set_xlim(b[0][0]-5, b[1][0]+5); ax.set_ylim(b[0][1]-5, b[1][1]+5)
for fx in mb.FSW_X: ax.add_patch(Circle((fx, -4.2-mb.WING_D+mb.WING_D*0.5-2), mb.FSW_HOLE/2, fc='red', alpha=.6)); ax.text(fx-6, -4.2-mb.WING_D*0.5-6, 'footswitch', fontsize=7)
ax.set_title('Top view: board (green) in the tray. Orange = connector holes, blue = wire holes, grey = standoffs at the 4 mounting holes. Red = footswitch holes in the front wing. Back edge (jacks) at top.')
plt.tight_layout(); plt.savefig(os.path.join(os.path.dirname(__file__), 'PureIR-base-preview.png'), dpi=110)
