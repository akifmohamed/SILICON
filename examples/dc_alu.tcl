# dc: synthesise the 8-bit ALU.
# run from the repository root:  bin/dc -f examples/dc_alu.tcl

read_verilog examples/rtl/alu.v
elaborate alu

create_clock -period 4.0 -name clk
set_input_delay 1.0
set_output_delay 1.0
set_clock_uncertainty 0.15
set_max_fanout 8
set_max_transition 0.30

compile

check_design
report_area
report_timing -max_paths 3
report_power
report_qor

# the netlist database is the handoff to icc2
write -format json     -output build/alu.json
write -format verilog  -output build/alu.v
write_sdc                        build/alu.sdc

# mapped netlist vs elapsed RTL, random vectors
verify -vectors 256 -seed 7
