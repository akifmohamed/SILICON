# icc2: implement the ALU from the netlist database written by dc.
# run from the repository root:  bin/icc2 -f examples/icc2_alu.tcl

read_netlist build/alu.json

initialize_floorplan -core_utilization 0.65 -aspect_ratio 1.0 -core_offset {2 2}
place_opt
clock_opt
route_auto

report_placement
report_clock_tree
report_drc
report_congestion
report_timing -max_paths 2
report_qor

write_def        build/alu.def
write_lef        build/alu.lef
write_library_lef build/cells.lef
write_tech_lef   build/tech.lef
write_gds        build/alu.gds
write_svg        build/alu.svg
