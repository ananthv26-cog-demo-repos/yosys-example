// const variables, let declarations
module p28_let_and_const (input logic [7:0] a, output logic y);
    const logic [7:0] K = 8'd7;
    let is_k(x) = (x == K);
    assign y = is_k(a);
endmodule
