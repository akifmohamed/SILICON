"""Design-rule structures used by the router and the checker.

Kept separate so the rule deck can be read, printed and reasoned about on its
own -- the checker never trusts the router's bookkeeping, it re-derives every
violation from the geometry that was written out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class MetalRule:
    """Manufacturing rules for one routing layer (synthetic values)."""

    layer: str
    direction: str              # "horizontal" | "vertical"
    width: float                # minimum drawn width (um)
    spacing: float              # minimum edge-to-edge spacing (um)
    min_area: float             # minimum metal area (um^2)
    via_size: float             # via cut (um)
    via_spacing: float          # via-to-via edge spacing (um)
    enclosure: float            # metal extension past a via (um)
    resistance: float = 0.12    # ohm per um
    capacitance: float = 0.00020  # pF per um


def default_rules() -> Dict[str, MetalRule]:
    """A 6-layer stack: M1..M6 with increasing width and decreasing resistance."""
    return {
        "M1": MetalRule("M1", "horizontal", 0.07, 0.07, 0.020, 0.05, 0.07, 0.020, 0.18, 0.00022),
        "M2": MetalRule("M2", "vertical",   0.07, 0.07, 0.020, 0.05, 0.07, 0.020, 0.12, 0.00020),
        "M3": MetalRule("M3", "horizontal", 0.08, 0.08, 0.025, 0.06, 0.08, 0.025, 0.10, 0.00020),
        "M4": MetalRule("M4", "vertical",   0.08, 0.08, 0.025, 0.06, 0.08, 0.025, 0.08, 0.00018),
        "M5": MetalRule("M5", "horizontal", 0.10, 0.10, 0.040, 0.07, 0.09, 0.030, 0.06, 0.00016),
        "M6": MetalRule("M6", "vertical",   0.10, 0.10, 0.040, 0.07, 0.09, 0.030, 0.05, 0.00015),
    }


# ---------------------------------------------------------------------------
# GDSII layer map
# ---------------------------------------------------------------------------

#: ``layer name -> (GDSII layer, datatype)``.  Metal and cut layers get realistic
#: numbers (one decade per metal level) instead of the placeholder 1..6 mapping
#: an early version used; the annotation layers live above 100 so they can never
#: be confused with manufacturing geometry.
GDS_LAYER_MAP: Dict[str, Tuple[int, int]] = {
    "M1": (10, 0), "M2": (20, 0), "M3": (30, 0),
    "M4": (40, 0), "M5": (50, 0), "M6": (60, 0),
    "V1": (11, 0), "V2": (21, 0), "V3": (31, 0),
    "V4": (41, 0), "V5": (51, 0), "V6": (61, 0),
    "CELL": (100, 0), "PIN": (101, 0), "OUTLINE": (102, 0),
    "LABEL": (103, 0),
}


def via_layer(lower: str, upper: str) -> str:
    """Name of the cut layer between two metal layers (``M2``/``M3`` -> ``V2``)."""
    try:
        return f"V{int(lower[1:])}"
    except (ValueError, IndexError):        # pragma: no cover - defensive
        return "V1"


def gds_layer(name: str) -> Tuple[int, int]:
    """``(layer, datatype)`` for a layer name, or the metal 1 default."""
    return GDS_LAYER_MAP.get(name, GDS_LAYER_MAP["M1"])


def layer_for_gds(number: int) -> Optional[str]:
    """Inverse map: GDSII layer number back to a layer name."""
    for name, (layer, _datatype) in GDS_LAYER_MAP.items():
        if layer == number:
            return name
    return None
