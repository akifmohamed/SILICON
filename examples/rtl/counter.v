module counter #(parameter WIDTH = 8) (
  input  wire clk,
  input  wire rst_n,
  input  wire en,
  output reg  [WIDTH-1:0] count,
  output wire              tick
);
  wire [WIDTH-1:0] nxt;

  assign nxt = count + {{(WIDTH-1){1'b0}}, en};
  assign tick = (count == {WIDTH{1'b1}});

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n)
      count <= {WIDTH{1'b0}};
    else if (en)
      count <= nxt;
  end
endmodule
