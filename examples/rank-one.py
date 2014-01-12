# SETUPBEGIN
import numpy as np
import pyopencl as cl
import loopy as lp

ctx = cl.create_some_context()
queue = cl.CommandQueue(ctx)

knl = lp.make_kernel(queue.device,
    "{[i,j]: 0<=i,j<n}",
    "c[i, j] = a[i]*b[j]",
    assumptions="n >= 16")

a = np.arange(200, dtype=np.float32)
b = np.arange(200, dtype=np.float32)

evt, (c,) = knl(queue, a=a, b=b, options="write_cl")
# SETUPEND

orig_knl = knl

# SPLITBEGIN
knl = lp.split_iname(knl, "i", 16,
        outer_tag="g.0", inner_tag="l.0")
knl = lp.split_iname(knl, "j", 16,
        outer_tag="g.1", inner_tag="l.1")
# SPLITEND

evt, (c,) = knl(queue, a=a, b=b, options="write_cl")

split_knl = knl

# PREFETCH1BEGIN
knl = lp.add_prefetch(knl, "a")
knl = lp.add_prefetch(knl, "b")
# PREFETCH1END

evt, (c,) = knl(queue, a=a, b=b, options="write_cl")

knl = split_knl

# PREFETCH2BEGIN
knl = lp.add_prefetch(knl, "a", ["i_inner"])
knl = lp.add_prefetch(knl, "b", ["j_inner"])
# PREFETCH2END

evt, (c,) = knl(queue, a=a, b=b, options="write_cl")

knl = orig_knl

# PREFETCH3BEGIN
knl = lp.split_iname(knl, "i", 256,
        outer_tag="g.0", slabs=(0, 1))
knl = lp.split_iname(knl, "j", 256,
        outer_tag="g.1", slabs=(0, 1))

knl = lp.add_prefetch(knl, "a", ["i_inner"], default_tag=None)
knl = lp.add_prefetch(knl, "b", ["j_inner"], default_tag=None)

knl = lp.split_iname(knl, "i_inner", 16,
        inner_tag="l.0")
knl = lp.split_iname(knl, "j_inner", 16,
        inner_tag="l.1")

knl = lp.split_iname(knl, "b_dim_0", 16,
        outer_tag="l.1", inner_tag="l.0")
knl = lp.split_iname(knl, "a_dim_0", 16,
        outer_tag="l.1", inner_tag="l.0")
# PREFETCH3END

evt, (c,) = knl(queue, a=a, b=b, options="write_cl")
