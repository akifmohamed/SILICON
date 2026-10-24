module alu #(parameter WIDTH = 8) (
  input  wire clk,
  input  wire rst_n,
  input  wire [WIDTH-1:0] a, b,
  input  wire [2:0] op,
  output reg  [WIDTH-1:0] y,
  output reg  zero
);
  reg [WIDTH-1:0] next_y;
  always @* begin
    case (op)
      3'd0: next_y = a + b;
      3'd1: next_y = a - b;
      3'd2: next_y = a & b;
      3'd3: next_y = a | b;
      3'd4: next_y = a ^ b;
      3'd5: next_y = (a < b) ? WIDTH'd1 : WIDTH'd0;
      default: next_y = a;
    endcase
  end
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      y <= WIDTH'd0;
      zero <= 1'b0;
    end else begin
      y <= next_y;
      zero <= (next_y == WIDTH'd0);
    end
  end
endmodule
