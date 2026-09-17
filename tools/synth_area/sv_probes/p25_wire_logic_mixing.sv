// var/wire keywords, tri, default_nettype none
`default_nettype none
module p25_wire_logic_mixing (input wire logic a, b, output var logic y);
    wire t = a & b;
    assign y = t;
endmodule
`default_nettype wire
