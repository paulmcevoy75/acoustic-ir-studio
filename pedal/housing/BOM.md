# Housing hardware list

| Qty | Part | Used for |
|---|---|---|
| 4 | **M3 x 8 mm screw**, pan head (or button head), steel or stainless | Holds the board to the four posts. Screws straight into the 2.8 mm pilot holes and cuts its own thread in the plastic. |
| 6 | Stick-on rubber feet, 12 mm diameter | Fit the 12 mm round recesses under the tray (4 under the tray, 2 under the footswitch wing). |
| 2 | M12 footswitches with their nuts and washers | Come with the switches; the 12.7 mm holes in the wing roof take the 12 mm thread. |

Why M3 x 8: the board is 1.6 mm thick and the post pilot hole is 8 mm deep. An 8 mm screw leaves about 6.4 mm of thread in the plastic and does not touch the bottom of the hole. Do not use anything longer than 9 mm. 6 mm is too short.

Notes
- Use a pan or button head (head about 5.5 to 6 mm). A countersunk head will not seat on the board.
- Tighten until snug only. Over-tightening strips the plastic thread.
- Optional: brass M3 heat-set inserts instead (about 4.6 mm long, 5.7 mm outside diameter). Set `HOLE_D` to 4.0 in `housing/make_base.py`, regenerate, and use M3 x 6 mm screws.
- The lid is a friction fit and needs no screws.
