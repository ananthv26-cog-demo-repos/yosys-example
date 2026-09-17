// Package with a packed struct payload and an enum, as used by struct_fifo.sv.
package fifo_pkg;
    typedef enum logic [1:0] {
        KIND_DATA = 2'd0,
        KIND_CTRL = 2'd1,
        KIND_LAST = 2'd2,
        KIND_ERR  = 2'd3
    } kind_e;

    typedef struct packed {
        kind_e       kind;
        logic [3:0]  tag;
        logic [15:0] payload;
    } entry_t;

    function automatic int unsigned ptr_width(input int unsigned depth);
        return (depth > 1) ? $clog2(depth) : 1;
    endfunction
endpackage
