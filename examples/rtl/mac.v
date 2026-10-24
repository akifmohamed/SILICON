module mac #(parameter WIDTH = 8) (
  input  wire             clk,
  input  wire             rst_n,
  input  wire             load,
  input  wire [WIDTH-1:0] a, b,
  output reg  [2*WIDTH-1:0] acc,
  output wire             overflow
);
  wire [2*WIDTH-1:0] product;
  wire [2*WIDTH-1:0] sum;

  assign product = a * b;
  assign sum     = load ? product : acc + product;
  assign overflow = acc[2*WIDTH-1];

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) acc <= {(2*WIDTH){1'b0}};
    else        acc <= sum;
  end
endmodule
