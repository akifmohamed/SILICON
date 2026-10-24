"""Layout writers: DEF, GDSII, SVG and a text view of the floorplan.

Everything here works from the objects the physical-design engines produce
(:class:`silicon.pnr.Floorplan`, :class:`~silicon.pnr.RouteResult`, placed
:class:`~silicon.netlist.GateCell` instances), so the files always describe the
geometry that was actually computed.

Geometry units are micrometres for DEF/SVG and nanometres inside GDSII, which
is what each format expects.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .library import Library
from .netlist import GateCell, GateNetlist
from .pnr import DrcChecker, Floorplan, RouteResult, Segment, Via
from .rules import GDS_LAYER_MAP, MetalRule, default_rules, gds_layer, layer_for_gds, via_layer

DBU = 1000                      # DEF database units per micron
GDS_DBU = 1e-9                  # one GDSII database unit = 1 nm

# GDSII layer numbers come from the technology map in rules.py
GDS_LAYERS = {name: number for name, (number, _dt) in GDS_LAYER_MAP.items()}
GDS_ROUTE_LAYERS = {layer: GDS_LAYERS[layer] for layer in ("M1", "M2", "M3", "M4", "M5", "M6")}
DEF_LAYERS = {"M1": "M1", "M2": "M2", "M3": "M3", "M4": "M4", "M5": "M5", "M6": "M6"}


# ---------------------------------------------------------------------------
# DEF
# ---------------------------------------------------------------------------


def _d(value: float) -> int:
    return int(round(value * DBU))


def write_def(path: str, netlist: GateNetlist, library: Library, plan: Floorplan,
              route: Optional[RouteResult], design_name: str = "",
              fillers: Sequence[GateCell] = ()) -> None:
    """Write a DEF 5.8 description of the placed and routed block."""
    name = design_name or netlist.top
    lines: List[str] = []
    add = lines.append
    add("VERSION 5.8 ;")
    add('DIVIDERCHAR "/" ;')
    add('BUSBITCHARS "[]" ;')
    add(f"DESIGN {name} ;")
    add(f"UNITS DISTANCE MICRONS {DBU} ;")
    add("")
    add(f"DIEAREA ( {_d(plan.die.x1)} {_d(plan.die.y1)} ) "
        f"( {_d(plan.die.x2)} {_d(plan.die.y2)} ) ;")
    add("")
    for index, row in enumerate(plan.rows):
        add(f"ROW ROW_{index} core {_d(row.x1)} {_d(row.y)} N "
            f"DO {row.sites} BY 1 STEP {_d(library.site_width)} 0 ;")
    add("")
    placed = [c for c in netlist.cells if c.width > 0] + list(fillers)
    add(f"COMPONENTS {len(placed)} ;")
    for cell in sorted(placed, key=lambda c: c.instance):
        add(f"    - {cell.instance} {cell.cell} + PLACED "
            f"( {_d(cell.x)} {_d(cell.y)} ) {cell.orientation or 'N'} ;")
    add("END COMPONENTS")
    add("")
    ports = sorted(netlist.port_dirs)
    add(f"PINS {len(ports)} ;")
    for port in ports:
        direction = netlist.port_dirs[port].upper()
        net = netlist.ports.get(port, port)
        if port in plan.io_pins:
            x, y, side = plan.io_pins[port]
            orient = {"W": "W", "E": "E", "N": "N", "S": "S"}.get(side, "N")
            add(f"    - {port} + NET {net} + DIRECTION {direction} + USE SIGNAL "
                f"+ LAYER M2 ( 0 0 ) ( 0 0 ) + PLACED ( {_d(x)} {_d(y)} ) {orient} ;")
        else:
            add(f"    - {port} + NET {net} + DIRECTION {direction} + USE SIGNAL ;")
    add("END PINS")
    add("")
    nets = sorted(netlist.nets)
    add(f"NETS {len(nets)} ;")
    for net in nets:
        keys = netlist.nets[net]
        conns: List[str] = []
        for key in sorted(keys):
            if key.startswith("port/"):
                conns.append(f"PIN {key[5:]}")
            else:
                inst, pin = key.split("/", 1)
                conns.append(f"{inst} {pin}")
        unique = sorted(set(conns))
        header = f"    - {net} ( " + " ) ( ".join(unique) + " )"
        routing = ""
        if route is not None:
            segments = [s for s in route.segments if s.net == net]
            if segments:
                routing = " + ROUTED " + _def_routing(segments)
        add(header + (" " + routing if routing else "") + " ;")
    add("END NETS")
    add("")
    add("END DESIGN")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _def_routing(segments: Sequence) -> str:
    """Group segments of one net into ``LAYER ( x y ) ( x y )`` chains."""
    parts: List[str] = []
    by_layer: Dict[str, List] = {}
    for seg in segments:
        by_layer.setdefault(seg.layer, []).append(seg)
    for layer in sorted(by_layer):
        group = by_layer[layer]
        group.sort(key=lambda s: (round(s.y1, 4), round(s.x1, 4)))
        for seg in group:
            parts.append(f"{DEF_LAYERS.get(layer, layer)} "
                         f"( {_d(seg.x1)} {_d(seg.y1)} ) ( {_d(seg.x2)} {_d(seg.y2)} )")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# GDSII
# ---------------------------------------------------------------------------

# record types
_HEADER, _BGNLIB, _LIBNAME, _UNITS, _ENDLIB, _BGNSTR, _STRNAME, _ENDSTR = (
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07)
_BOUNDARY, _PATH, _SREF, _TEXT, _LAYER, _DATATYPE, _WIDTH, _XY = (
    0x08, 0x09, 0x0A, 0x0C, 0x0D, 0x0E, 0x0F, 0x10)
_ENDEL, _SNAME, _TEXTTYPE, _STRING, _MAG, _ANGLE, _PATHTYPE, _BGNEXTN, _ENDEXTN = (
    0x11, 0x12, 0x16, 0x19, 0x1A, 0x1B, 0x21, 0x30, 0x31)


def _record(rec_type: int, data_type: int, payload: bytes = b"") -> bytes:
    return struct.pack(">HBB", 4 + len(payload), rec_type, data_type) + payload


def _i2(value: int) -> bytes:
    return struct.pack(">h", value)


def _i4(value: int) -> bytes:
    return struct.pack(">i", value)


def _gds_real(value: float) -> bytes:
    """Encode a float in the GDSII 8-byte excess-64 hexadecimal format."""
    if value == 0:
        return b"\x00" * 8
    sign = 0x80 if value < 0 else 0x00
    value = abs(value)
    exponent = 64
    while value >= 1.0:
        value /= 16.0
        exponent += 1
    while value < 1.0 / 16.0:
        value *= 16.0
        exponent -= 1
    mantissa = int(round(value * (1 << 56)))
    if mantissa >= (1 << 56):                     # rounding carry
        mantissa >>= 4
        exponent += 1
    return bytes([sign | (exponent & 0x7F)]) + mantissa.to_bytes(7, "big")


def _timestamp() -> bytes:
    # a fixed, valid timestamp keeps the output byte-identical between runs
    return b"".join(_i2(v) for v in (2024, 1, 1, 0, 0, 0,
                                    2024, 1, 1, 0, 0, 0))


def _nm(value: float) -> int:
    return int(round(value * 1000.0))


def _boundary(layer: int, datatype: int, points: Sequence[Tuple[float, float]]) -> bytes:
    payload = _record(_LAYER, 2, _i2(layer)) + _record(_DATATYPE, 2, _i2(datatype))
    coords = b"".join(_i4(_nm(x)) + _i4(_nm(y)) for x, y in points)
    payload += _record(_XY, 3, coords)
    return _record(_BOUNDARY, 0) + payload + _record(_ENDEL, 0)


def _path(layer: int, datatype: int, points: Sequence[Tuple[float, float]],
          width_nm: int) -> bytes:
    payload = _record(_LAYER, 2, _i2(layer)) + _record(_DATATYPE, 2, _i2(datatype))
    payload += _record(_PATHTYPE, 2, _i2(0)) + _record(_WIDTH, 3, _i4(width_nm))
    coords = b"".join(_i4(_nm(x)) + _i4(_nm(y)) for x, y in points)
    payload += _record(_XY, 3, coords)
    return _record(_PATH, 0) + payload + _record(_ENDEL, 0)


def _sref(name: str, x: float, y: float, rotate: int = 0) -> bytes:
    payload = _record(_SNAME, 6, name.encode("ascii") + b"\x00")
    if rotate % 360:
        payload += _record(_ANGLE, 5, _gds_real(float(rotate % 360)))
    payload += _record(_XY, 3, _i4(_nm(x)) + _i4(_nm(y)))
    return _record(_SREF, 0) + payload + _record(_ENDEL, 0)


def _text(layer: int, x: float, y: float, string: str) -> bytes:
    payload = _record(_LAYER, 2, _i2(layer)) + _record(_TEXTTYPE, 2, _i2(0))
    payload += _record(_MAG, 5, _gds_real(1.0))
    payload += _record(_XY, 3, _i4(_nm(x)) + _i4(_nm(y)))
    payload += _record(_STRING, 6, string.encode("ascii") + b"\x00")
    return _record(_TEXT, 0) + payload + _record(_ENDEL, 0)


def _structure(name: str, elements: Iterable[bytes]) -> bytes:
    body = _record(_BGNSTR, 2, _timestamp())
    body += _record(_STRNAME, 6, name.encode("ascii") + b"\x00")
    for element in elements:
        body += element
    body += _record(_ENDSTR, 0)
    return body


def _cell_structure(name: str, width: float, height: float) -> bytes:
    """Geometry for one standard cell: outline, active area and pin markers."""
    body = [
        _boundary(GDS_LAYERS["CELL"], 0,
                  [(0, 0), (width, 0), (width, height), (0, height), (0, 0)]),
        _boundary(GDS_LAYERS["M1"], 0,
                  [(0.02, 0.02), (width - 0.02, 0.02), (width - 0.02, height - 0.02),
                   (0.02, height - 0.02), (0.02, 0.02)]),
    ]
    return _structure(name, body)


def write_gds(path: str, netlist: GateNetlist, library: Library, plan: Floorplan,
              route: Optional[RouteResult], design_name: str = "",
              fillers: Sequence[GateCell] = ()) -> None:
    """Write a GDSII stream file: one structure per cell plus the top block."""
    name = design_name or netlist.top
    placed = [c for c in netlist.cells if c.width > 0] + list(fillers)
    cells: Dict[str, Tuple[float, float]] = {}
    for cell in placed:
        model = library.cell(cell.cell)
        if cell.cell not in cells:
            cells[cell.cell] = (model.width, model.row_height)

    out = bytearray()
    out += _record(_HEADER, 2, _i2(600))
    out += _record(_BGNLIB, 2, _timestamp())
    out += _record(_LIBNAME, 6, b"SILICON" + b"\x00")
    out += _record(_UNITS, 5, _gds_real(GDS_DBU / 1e-6) + _gds_real(GDS_DBU))

    for cell_name in sorted(cells):
        width, height = cells[cell_name]
        out += _cell_structure(cell_name, width, height)

    top: List[bytes] = []
    for cell in sorted(placed, key=lambda c: c.instance):
        rotate = {"N": 0, "S": 180, "E": 90, "W": 270}.get(cell.orientation or "N", 0)
        top.append(_sref(cell.cell, cell.x, cell.y, rotate=rotate))
    if route is not None:
        for seg in route.segments:
            layer = GDS_ROUTE_LAYERS.get(seg.layer, 2)
            top.append(_path(layer, 0, [(seg.x1, seg.y1), (seg.x2, seg.y2)],
                             _nm(seg.width)))
        for via in route.vias:
            half = via.size / 2
            cut = GDS_LAYERS.get(via_layer(*via.layers), GDS_LAYERS["V1"])
            top.append(_boundary(cut, 0,
                                 [(via.x - half, via.y - half), (via.x + half, via.y - half),
                                  (via.x + half, via.y + half), (via.x - half, via.y + half),
                                  (via.x - half, via.y - half)]))
        labelled = set()
        for seg in route.segments:            # one net label per routed net
            if seg.net in labelled:
                continue
            labelled.add(seg.net)
            top.append(_text(GDS_LAYERS["LABEL"],
                             round((seg.x1 + seg.x2) / 2, 4),
                             round((seg.y1 + seg.y2) / 2, 4), seg.net))
    top.append(_boundary(GDS_LAYERS["OUTLINE"], 0,
                         [(plan.die.x1, plan.die.y1), (plan.die.x2, plan.die.y1),
                          (plan.die.x2, plan.die.y2), (plan.die.x1, plan.die.y2),
                          (plan.die.x1, plan.die.y1)]))
    top.append(_text(GDS_LAYERS["LABEL"], plan.die.x1, plan.die.y2 + 1.0, f"{name}"))
    out += _structure(name, top)
    out += _record(_ENDLIB, 0)

    with open(path, "wb") as handle:
        handle.write(bytes(out))


def _gds_real_value(raw: bytes) -> float:
    """Decode one GDSII 8-byte excess-64 real."""
    if len(raw) < 8:
        return 0.0
    exponent = raw[0] & 0x7F
    mantissa = int.from_bytes(raw[1:8], "big")
    value = mantissa / float(1 << 56)
    return -value * 16.0 ** (exponent - 64) if raw[0] & 0x80 \
        else value * 16.0 ** (exponent - 64)


@dataclass
class GdsShape:
    """One piece of geometry read back out of a GDSII stream file."""

    structure: str
    layer: str
    datatype: int
    kind: str                       # "path" | "boundary"
    points: List[Tuple[float, float]]      # micrometres, flattened
    width: float = 0.0              # micrometres, paths only
    text: str = ""                  # TEXT records: the label string

    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return (min(xs), min(ys), max(xs), max(ys))

    @property
    def size(self) -> float:
        x1, y1, x2, y2 = self.bbox
        return min(x2 - x1, y2 - y1)


def read_gds_geometry(path: str) -> List[GdsShape]:
    """Read back every path and boundary, flattening SREF placements.

    This is what lets the design-rule check run on the *stream file* instead of
    on the in-memory model: the numbers come out of the bytes a mask shop would
    receive.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    structures: Dict[str, List[GdsShape]] = {}
    srefs: Dict[str, List[Tuple[str, float, float, int]]] = {}
    order: List[str] = []
    scale = 0.001                       # um per database unit, from UNITS
    current: Optional[str] = None
    kind: Optional[str] = None
    pending: Dict[str, object] = {}
    pos = 0
    while pos + 4 <= len(data):
        length, rec_type, _dtype = struct.unpack(">HBB", data[pos:pos + 4])
        if length < 4 or pos + length > len(data):
            break
        payload = data[pos + 4:pos + length]
        pos += length
        if rec_type == _UNITS and len(payload) >= 16:
            scale = _gds_real_value(payload[0:8])
        elif rec_type == _STRNAME:
            current = payload.rstrip(b"\x00").decode("ascii", "replace")
            structures.setdefault(current, [])
            srefs.setdefault(current, [])
            order.append(current)
        elif rec_type == _ENDSTR:
            current = None
            kind = None
        elif rec_type in (_PATH, _BOUNDARY, _SREF, _TEXT):
            kind = {_PATH: "path", _BOUNDARY: "boundary",
                    _SREF: "sref", _TEXT: "text"}[rec_type]
            pending = {}
        elif rec_type == _LAYER:
            pending["layer"] = struct.unpack(">h", payload[:2])[0]
        elif rec_type == _DATATYPE:
            pending["datatype"] = struct.unpack(">h", payload[:2])[0]
        elif rec_type == _PATHTYPE:
            pending["pathtype"] = struct.unpack(">h", payload[:2])[0]
        elif rec_type == _WIDTH and len(payload) >= 4:
            pending["width"] = struct.unpack(">i", payload[:4])[0]
        elif rec_type == _SNAME:
            pending["sname"] = payload.rstrip(b"\x00").decode("ascii", "replace")
        elif rec_type == _ANGLE:
            pending["angle"] = _gds_real_value(payload)
        elif rec_type == _XY:
            values = [struct.unpack(">i", payload[i:i + 4])[0]
                      for i in range(0, len(payload) - 3, 4)]
            pending["xy"] = [(values[i] * scale, values[i + 1] * scale)
                             for i in range(0, len(values) - 1, 2)]
        elif rec_type == _STRING:
            pending["text"] = payload.rstrip(b"\x00").decode("ascii", "replace")
        elif rec_type == _ENDEL:
            if kind == "text" and current is not None and pending.get("xy"):
                number = int(pending.get("layer", 0))
                structures[current].append(GdsShape(
                    structure=current,
                    layer=layer_for_gds(number) or f"L{number}",
                    datatype=int(pending.get("datatype", 0)),
                    kind="text", points=list(pending["xy"]),
                    text=str(pending.get("text", ""))))
            elif kind in ("path", "boundary") and current is not None \
                    and pending.get("xy"):
                number = int(pending.get("layer", 0))
                structures[current].append(GdsShape(
                    structure=current,
                    layer=layer_for_gds(number) or f"L{number}",
                    datatype=int(pending.get("datatype", 0)),
                    kind=kind,
                    points=list(pending["xy"]),
                    width=abs(int(pending.get("width", 0))) * scale))
            elif kind == "sref" and current is not None and pending.get("xy"):
                point = pending["xy"][0]
                angle = int(round(float(pending.get("angle", 0.0))))
                srefs[current].append((str(pending.get("sname", "")),
                                       point[0], point[1], angle))
            kind = None
            pending = {}
    # flatten: the last structure in the file is the top cell
    shapes: List[GdsShape] = []

    def flatten(name: str, dx: float, dy: float, angle: int, depth: int = 0) -> None:
        if depth > 8:
            return
        for shape in structures.get(name, []):
            points = []
            for x, y in shape.points:
                for _ in range((angle % 360) // 90):
                    x, y = -y, x
                points.append((x + dx, y + dy))
            shapes.append(GdsShape(structure=shape.structure, layer=shape.layer,
                                   datatype=shape.datatype, kind=shape.kind,
                                   points=points, width=shape.width,
                                   text=shape.text))
        for child, x, y, child_angle in srefs.get(name, []):
            fx, fy = x, y
            for _ in range((angle % 360) // 90):
                fx, fy = -fy, fx
            flatten(child, fx + dx, fy + dy, (angle + child_angle) % 360, depth + 1)

    if order:
        flatten(order[-1], 0.0, 0.0, 0)
    return shapes


def check_gds(path: str, rules: Optional[Dict[str, MetalRule]] = None,
              layers: Optional[Sequence[str]] = None) -> Dict[str, List[str]]:
    """Design-rule check the *written* GDSII stream, with connectivity extraction.

    The layout is flattened (SREF transforms applied), then shapes that touch on
    the same layer -- or that meet through a cut square -- are grouped into
    connected components, and the net labels written into the stream are used to
    name them. That is what separates a legal corner of one net (shapes touching,
    one label) from a short (shapes touching, two labels).

    Checked: minimum width, minimum area, spacing, shorts, cut size and cut
    spacing on the routing layers. Cell-internal M1 is out of scope: this
    library's cell geometry is a placeholder rectangle, so the port is skipped
    rather than reported as thousands of false violations.
    """
    rules = rules or default_rules()
    checked = [layer for layer in (layers or [name for name in rules if name != "M1"])
               if layer in GDS_LAYER_MAP]
    found: Dict[str, List[str]] = {}

    def note(rule: str, text: str) -> None:
        found.setdefault(rule, []).append(text)

    shapes = read_gds_geometry(path)
    metal = [s for s in shapes if s.layer in checked
             and s.kind in ("path", "boundary")]
    cuts = [s for s in shapes if s.layer.startswith("V")
            and s.layer in GDS_LAYER_MAP and s.kind == "boundary"]
    labels = [s for s in shapes if s.kind == "text" and s.text]

    # -- width and area, shape by shape --------------------------------
    for shape in metal:
        rule = rules[shape.layer]
        x1, y1, x2, y2 = shape.bbox
        if shape.kind == "path":
            length = max(x2 - x1, y2 - y1)
            if shape.width + 1e-6 < rule.width:
                note("WIDTH", f"{shape.layer} path width {shape.width:.3f} < "
                              f"{rule.width:.3f} at ({x1:.2f}, {y1:.2f})")
            if length * shape.width + 1e-9 < rule.min_area:
                note("MIN_AREA", f"{shape.layer} path area "
                                 f"{length * shape.width:.4f} < {rule.min_area:.4f}")
        else:
            if shape.size + 1e-6 < rule.width:
                note("WIDTH", f"{shape.layer} rectangle {shape.size:.3f} < "
                              f"{rule.width:.3f}")
            if (x2 - x1) * (y2 - y1) + 1e-9 < rule.min_area:
                note("MIN_AREA", f"{shape.layer} rectangle area "
                                 f"{(x2 - x1) * (y2 - y1):.4f} < {rule.min_area:.4f}")

    # -- connectivity: union-find over touching shapes ------------------
    parent = list(range(len(shapes)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    def boxes_touch(a: GdsShape, b: GdsShape) -> bool:
        ax1, ay1, ax2, ay2 = a.bbox
        bx1, by1, bx2, by2 = b.bbox
        return ax1 <= bx2 + 1e-9 and bx1 <= ax2 + 1e-9 and \
            ay1 <= by2 + 1e-9 and by1 <= ay2 + 1e-9

    # same-layer touching shapes
    by_layer: Dict[str, List[int]] = {}
    for index, shape in enumerate(shapes):
        if shape.kind in ("path", "boundary") and shape.layer in checked:
            by_layer.setdefault(shape.layer, []).append(index)
    for layer, indices in by_layer.items():
        for position, a in enumerate(indices):
            for b in indices[position + 1:]:
                if boxes_touch(shapes[a], shapes[b]):
                    union(a, b)
    # metal reached through a cut: Vn joins Mn and Mn+1
    for cut in cuts:
        number = int(cut.layer[1:]) if cut.layer[1:].isdigit() else 1
        pair = {f"M{number}", f"M{number + 1}"}
        touched = [index for index, shape in enumerate(shapes)
                   if shape.kind in ("path", "boundary")
                   and shape.layer in pair and boxes_touch(shape, cut)]
        for index in touched[1:]:
            union(touched[0], index)
    # labels name their component
    names: Dict[int, set] = {}
    for label in labels:
        point = (label.points[0][0], label.points[0][1])
        for index, shape in enumerate(shapes):
            if shape.kind not in ("path", "boundary") or shape.layer not in checked:
                continue
            x1, y1, x2, y2 = shape.bbox
            if x1 - 0.05 <= point[0] <= x2 + 0.05 and \
                    y1 - 0.05 <= point[1] <= y2 + 0.05:
                names.setdefault(find(index), set()).add(label.text)
                break

    # -- shorts and spacing --------------------------------------------
    for layer, indices in by_layer.items():
        rule = rules[layer]
        for position, a_index in enumerate(indices):
            a = shapes[a_index]
            ax1, ay1, ax2, ay2 = a.bbox
            for b_index in indices[position + 1:]:
                b = shapes[b_index]
                bx1, by1, bx2, by2 = b.bbox
                if bx1 - ax2 > rule.spacing:
                    break
                dx = max(bx1 - ax2, ax1 - bx2, 0.0)
                dy = max(by1 - ay2, ay1 - by2, 0.0)
                gap = math.hypot(dx, dy)
                same_component = find(a_index) == find(b_index)
                if gap <= 1e-9:
                    if not same_component:
                        note("SHORT", f"{layer}: unconnected metal at "
                                      f"({ax1:.2f}, {ay1:.2f})")
                    continue
                if gap + 1e-6 < rule.spacing and not same_component:
                    note("SPACING", f"{layer} gap {gap:.3f} < {rule.spacing:.3f} "
                                    f"at ({ax1:.2f}, {ay1:.2f})")

    # -- cuts -----------------------------------------------------------
    for cut in cuts:
        index = int(cut.layer[1:]) if cut.layer[1:].isdigit() else 1
        rule = rules.get(f"M{index}") or rules[sorted(rules)[0]]
        if cut.size + 1e-6 < rule.via_size:
            note("VIA_SIZE", f"{cut.layer} cut {cut.size:.3f} < {rule.via_size:.3f}")
    cut_layers: Dict[str, List[GdsShape]] = {}
    for cut in cuts:
        cut_layers.setdefault(cut.layer, []).append(cut)
    for layer, group in sorted(cut_layers.items()):
        index = int(layer[1:]) if layer[1:].isdigit() else 1
        rule = rules.get(f"M{index}") or rules[sorted(rules)[0]]
        group.sort(key=lambda sh: (sh.bbox[0], sh.bbox[1]))
        for position, a in enumerate(group):
            ax1, ay1, ax2, ay2 = a.bbox
            for b in group[position + 1:]:
                bx1, by1, bx2, by2 = b.bbox
                if bx1 - ax2 > rule.via_spacing:
                    break
                dx = max(bx1 - ax2, ax1 - bx2, 0.0)
                dy = max(by1 - ay2, ay1 - by2, 0.0)
                if math.hypot(dx, dy) + 1e-6 < rule.via_spacing:
                    note("VIA_SPACING", f"{layer} cuts {math.hypot(dx, dy):.3f} "
                                        f"apart < {rule.via_spacing:.3f}")
    return found


def read_gds_summary(path: str) -> Dict[str, int]:
    """Read a GDSII file back and count record types (a writer self-check)."""
    counts = {"structures": 0, "boundaries": 0, "paths": 0, "srefs": 0,
              "texts": 0, "elements": 0, "record_bytes": 0}
    with open(path, "rb") as handle:
        data = handle.read()
    counts["record_bytes"] = len(data)
    pos = 0
    while pos + 4 <= len(data):
        length, rec_type, _dtype = struct.unpack(">HBB", data[pos:pos + 4])
        if length < 4:
            break
        payload = data[pos + 4:pos + length]
        if rec_type == _BGNSTR:
            counts["structures"] += 1
        elif rec_type == _BOUNDARY:
            counts["boundaries"] += 1
        elif rec_type == _PATH:
            counts["paths"] += 1
        elif rec_type == _SREF:
            counts["srefs"] += 1
        elif rec_type == _TEXT:
            counts["texts"] += 1
        pos += length
    counts["elements"] = counts["boundaries"] + counts["paths"] + counts["srefs"] + counts["texts"]
    return counts


# ---------------------------------------------------------------------------
# SVG view
# ---------------------------------------------------------------------------

_PALETTE = {
    "sequential": ("#f5a524", "#7c4a00"),
    "buffer": ("#4ade80", "#14532d"),
    "clock": ("#38bdf8", "#0c4a6e"),
    "combinational": ("#a78bfa", "#4c1d95"),
    "filler": ("#334155", "#1e293b"),
}
_LAYER_COLOURS = {"M2": "#22d3ee", "M3": "#f472b6", "M4": "#60a5fa", "M5": "#facc15",
                  "M1": "#64748b", "M6": "#a3e635"}


def _classify(cell: GateCell, clock_buffers: Iterable[str]) -> str:
    if cell.cell in clock_buffers or cell.origin == "cts":
        return "clock"
    if cell.cell.startswith("FILL"):
        return "filler"
    if cell.cell.startswith("DFF"):
        return "sequential"
    if cell.cell.startswith(("BUF", "INV")):
        return "buffer"
    return "combinational"


def write_svg(path: str, netlist: GateNetlist, library: Library, plan: Floorplan,
              route: Optional[RouteResult], design_name: str = "",
              title: str = "", stats: Optional[Dict[str, str]] = None,
              width_px: int = 1400, fillers: Sequence[GateCell] = ()) -> None:
    """Render the implemented layout to a self-contained SVG view.

    Cells are drawn first, wires on top of them (the way a layout viewer shows
    metal over the cell rows), then pins, rows and a legend including a scale
    bar. Everything is inline: no external fonts, images or stylesheets.
    """
    name = design_name or netlist.top
    die, core = plan.die, plan.core
    scale = min((width_px - 120) / die.width, 1200.0 / die.height)
    width_px = int(die.width * scale + 120)
    height_px = int(die.height * scale + 170)

    def sx(x: float) -> float:
        return 60 + (x - die.x1) * scale

    def sy(y: float) -> float:
        # SVG y grows downwards, layout y grows upwards
        return 74 + (die.y2 - y) * scale

    clock_names = {c.cell for c in netlist.cells if c.origin == "cts"}
    drawn = [c for c in netlist.cells if c.width > 0] + list(fillers)
    svg: List[str] = []
    add = svg.append
    add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width_px}" '
        f'height="{height_px}" viewBox="0 0 {width_px} {height_px}" '
        f'font-family="ui-monospace,SFMono-Regular,Menlo,monospace">')
    add('<defs><style>'
        '.s{fill:#94a3b8}.h{fill:#f8fafc;font-weight:700}.k{fill:#cbd5e1}'
        '.c{stroke:#0b1120;stroke-width:0.25}'
        '</style></defs>')
    add(f'<rect width="{width_px}" height="{height_px}" fill="#0b1120"/>')
    # die and core
    add(f'<rect x="{sx(die.x1):.2f}" y="{sy(die.y2):.2f}" '
        f'width="{die.width * scale:.2f}" height="{die.height * scale:.2f}" '
        f'fill="#0f172a" stroke="#475569" stroke-width="1"/>')
    add(f'<rect x="{sx(core.x1):.2f}" y="{sy(core.y2):.2f}" '
        f'width="{core.width * scale:.2f}" height="{core.height * scale:.2f}" '
        f'fill="#111c33" stroke="#334155"/>')
    # cell rows (faint guides) and cells
    for row in plan.rows:
        add(f'<line x1="{sx(row.x1):.2f}" y1="{sy(row.y):.2f}" '
            f'x2="{sx(row.x2):.2f}" y2="{sy(row.y):.2f}" '
            f'stroke="#1e293b" stroke-width="0.6"/>')
    logic = [c for c in drawn if not c.cell.startswith("FILL")]
    fill = [c for c in drawn if c.cell.startswith("FILL")]
    for cell in fill:
        add(f'<rect class="c" x="{sx(cell.x):.2f}" y="{sy(cell.y + cell.height):.2f}" '
            f'width="{max(cell.width * scale, 0.7):.2f}" '
            f'height="{max(cell.height * scale - 0.5, 0.7):.2f}" '
            f'fill="{_PALETTE["filler"][0]}" fill-opacity="0.45" '
            f'stroke="{_PALETTE["filler"][1]}"/>')
    for cell in sorted(logic, key=lambda c: (c.y, c.x)):
        kind = _classify(cell, clock_names)
        colour, stroke = _PALETTE[kind]
        add(f'<rect class="c" x="{sx(cell.x):.2f}" y="{sy(cell.y + cell.height):.2f}" '
            f'width="{max(cell.width * scale, 0.7):.2f}" '
            f'height="{max(cell.height * scale - 0.5, 0.7):.2f}" '
            f'fill="{colour}" fill-opacity="0.90" stroke="{stroke}"/>')
    # routing, drawn above the cells like metal in a layout viewer
    if route is not None:
        for layer, opacity, width in (("M2", 0.55, 0.9), ("M4", 0.55, 0.9),
                                      ("M3", 0.75, 1.1), ("M5", 0.75, 1.1)):
            for seg in route.segments:
                if seg.layer != layer:
                    continue
                colour = _LAYER_COLOURS.get(seg.layer, "#64748b")
                if seg.horizontal:
                    y = sy(seg.y1)
                    add(f'<line x1="{sx(min(seg.x1, seg.x2)):.2f}" y1="{y:.2f}" '
                        f'x2="{sx(max(seg.x1, seg.x2)):.2f}" y2="{y:.2f}" '
                        f'stroke="{colour}" stroke-width="{width}" '
                        f'stroke-opacity="{opacity}" stroke-linecap="round"/>')
                else:
                    x = sx(seg.x1)
                    add(f'<line x1="{x:.2f}" y1="{sy(min(seg.y1, seg.y2)):.2f}" '
                        f'x2="{x:.2f}" y2="{sy(max(seg.y1, seg.y2)):.2f}" '
                        f'stroke="{colour}" stroke-width="{width}" '
                        f'stroke-opacity="{opacity}" stroke-linecap="round"/>')
        for via in route.vias:
            add(f'<rect x="{sx(via.x) - 1:.2f}" y="{sy(via.y) - 1:.2f}" width="2" '
                f'height="2" fill="#e2e8f0" fill-opacity="0.8"/>')
    # IO pins
    for port, (x, y, side) in plan.io_pins.items():
        add(f'<rect x="{sx(x) - 2.5:.2f}" y="{sy(y) - 2.5:.2f}" width="5" height="5" '
            f'fill="#e2e8f0"/>')
    # titles
    add(f'<text class="h" x="60" y="30" font-size="19">{title or name}'
        f' -- physical implementation</text>')
    logic_count = len([c for c in netlist.cells
                       if c.width > 0 and not c.cell.startswith("FILL")])
    add(f'<text class="s" x="60" y="50" font-size="12">die '
        f'{die.width:.2f} x {die.height:.2f} um   core {core.width:.2f} x '
        f'{core.height:.2f} um   {plan.row_count} rows   {logic_count} logic cells'
        f'   {len(fill)} fillers</text>')
    if stats:
        text = "     ".join(f"{k}: {v}" for k, v in stats.items())
        add(f'<text class="s" x="{width_px - 60}" y="30" font-size="12" '
            f'text-anchor="end">{text}</text>')
    # legend
    legend_y = height_px - 34
    x = 60.0
    for kind, (colour, _stroke) in _PALETTE.items():
        add(f'<rect x="{x:.1f}" y="{legend_y - 9}" width="11" height="11" '
            f'fill="{colour}" fill-opacity="0.9"/>')
        add(f'<text class="s" x="{x + 16:.1f}" y="{legend_y}" font-size="11">'
            f'{kind}</text>')
        x += 28 + 6.6 * len(kind)
    if route is not None:
        for layer in sorted(route.layer_usage):
            colour = _LAYER_COLOURS.get(layer, "#64748b")
            label = f"{layer} {route.layer_usage[layer]:.0f} um"
            add(f'<rect x="{x:.1f}" y="{legend_y - 9}" width="11" height="3" '
                f'fill="{colour}"/>')
            add(f'<text class="s" x="{x + 16:.1f}" y="{legend_y}" font-size="11">'
                f'{label}</text>')
            x += 28 + 6.2 * len(label)
    # scale bar: 5 um
    bar = 5.0 if die.width > 12 else 1.0
    bx, by = width_px - 60 - bar * scale, height_px - 28
    add(f'<rect x="{bx:.2f}" y="{by:.2f}" width="{bar * scale:.2f}" height="2" '
        f'fill="#94a3b8"/>')
    add(f'<text class="k" x="{bx + bar * scale / 2:.2f}" y="{by - 4:.2f}" '
        f'font-size="10" text-anchor="middle">{bar:g} um</text>')
    add("</svg>")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(svg) + "\n")


# ---------------------------------------------------------------------------
# Text view
# ---------------------------------------------------------------------------


def ascii_layout(netlist: GateNetlist, plan: Floorplan, width: int = 98,
                 height: int = 26) -> str:
    """A coarse text map of the core: one character per grid bucket."""
    core = plan.core
    grid = [[" "] * width for _ in range(height)]

    def plot(x: float, y: float, char: str) -> None:
        col = int((x - core.x1) / max(core.width, 1e-9) * (width - 1))
        row = int((core.y2 - y) / max(core.height, 1e-9) * (height - 1))
        if 0 <= col < width and 0 <= row < height:
            grid[row][col] = char

    for cell in netlist.cells:
        if cell.width <= 0:
            continue
        char = "#"
        if cell.cell.startswith("DFF"):
            char = "F"
        elif cell.origin == "cts":
            char = "C"
        elif cell.cell.startswith("FILL"):
            char = "."
        for x in (cell.x, cell.x + cell.width / 2, cell.x + cell.width):
            plot(x, cell.y + cell.height / 2, char)
    border = "+" + "-" * width + "+"
    body = "\n".join("|" + "".join(row) + "|" for row in grid)
    note = (f"core {core.width:.1f} x {core.height:.1f} um, "
            f"{plan.row_count} rows, # logic  F flop  C clock buffer  . filler")
    return border + "\n" + body + "\n" + border + "\n" + note
