"""silicon: a small, honest re-implementation of a logic-synthesis tool and a
physical-design tool.

Two independent engines live here:

* ``dc_shell``  -- RTL to gate netlist (``verilog`` -> ``elaborate`` -> ``synth``
  -> ``sta``), with a JSON netlist database as the handoff artefact.
* ``icc2_shell`` -- gate netlist to placed and routed layout (``pnr`` ->
  ``writers``), reading the JSON database written by ``dc_shell`` (or any file
  in that format).

The standard-cell library in ``lib/stdcells_32nm.lib`` is SYNTHETIC: the cells,
areas and timings are made up for the purpose of exercising the engines. Nothing
here is a foundry library and no number produced by this package is a claim
about real silicon.
"""

VERSION = "0.2.0"
__version__ = VERSION
