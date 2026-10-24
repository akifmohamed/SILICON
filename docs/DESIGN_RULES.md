# Technology rule deck (synthetic)

`silicon/rules.py` owns the manufacturing rules the router targets and the checker
enforces. The numbers are synthetic but internally consistent: the 0.20 um track
pitch is legal against every spacing rule, and the cell library was generated on the
same 32 nm-style scale.

| Layer | Dir | Width (um) | Spacing (um) | Min area (um^2) | Cut (um) | Cut sp. (um) | Encl. (um) | R (ohm/um) | C (pF/um) | GDS layer/dt |
|---|---|---|---|---|---|---|---|---|---|---|
| M1 | hori | 0.070 | 0.070 | 0.020 | 0.050 | 0.070 | 0.020 | 0.180 | 0.000220 | 10/0 |
| M2 | vert | 0.070 | 0.070 | 0.020 | 0.050 | 0.070 | 0.020 | 0.120 | 0.000200 | 20/0 |
| M3 | hori | 0.080 | 0.080 | 0.025 | 0.060 | 0.080 | 0.025 | 0.100 | 0.000200 | 30/0 |
| M4 | vert | 0.080 | 0.080 | 0.025 | 0.060 | 0.080 | 0.025 | 0.080 | 0.000180 | 40/0 |
| M5 | hori | 0.100 | 0.100 | 0.040 | 0.070 | 0.090 | 0.030 | 0.060 | 0.000160 | 50/0 |
| M6 | vert | 0.100 | 0.100 | 0.040 | 0.070 | 0.090 | 0.030 | 0.050 | 0.000150 | 60/0 |

## Rules the router honours (and the checker re-derives)

* **WIDTH** - drawn width must be at least the layer minimum.
* **MIN_AREA** - short wires are widened (up to 3x width, bounded by neighbour spacing);
  anything that still cannot comply is reported, not hidden.
* **SPACING** - edge-to-edge distance between parallel wires on the same layer, plus
  corner spacing between perpendicular wires.
* **SHORT** - same-layer overlap between wires of different nets.
* **VIA_ENCLOSURE** - a cut must sit on metal, with the required enclosure past the wire
  end on both layers, and must not stick out sideways.
* **VIA_SIZE** - a cut must be at least the larger of the two layers' cut sizes.
* **VIA_SPACING** - cut-to-cut distance, including diagonal.

## Where each rule is checked

| Stage | Code | What it sees |
|---|---|---|
| After routing | `pnr.DrcChecker` | segments and vias in the router's geometry model, with net identity |
| After writing | `writers.check_gds` | the flattened GDSII stream: connectivity from touching
  shapes and cut-connected layers, nets named by the labels written into the stream |
| Placement | `Placer.congestion_map` | bin-level demand/supply, reported by `report_congestion` |

## Changing the deck

```python
from silicon.rules import MetalRule, default_rules
rules = dict(default_rules())
rules["M3"] = MetalRule("M3", "horizontal", 0.07, 0.09, 0.025, 0.06, 0.08, 0.03, 0.15, 2.1e-4)
cfg = PhysicalConfig(rules=rules)
```

The router refuses to start when a spacing rule is wider than the track pitch,
because wires could not be placed legally at all:

```
PhysicalError: track pitch 0.2 on M3 is smaller than its spacing rule 0.30
```
