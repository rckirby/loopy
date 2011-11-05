
def test_advect(ctx_factory):

    dtype = np.float32
    ctx = ctx_factory()
    order = "C"
    queue = cl.CommandQueue(ctx,
            properties=cl.command_queue_properties.PROFILING_ENABLE)

    N = 8
    M = 8

    from pymbolic import var
    K_sym = var("K")

    field_shape = (N, N, N, K_sym)
    interim_field_shape = (M, M, M, K_sym)

    # 1. direction-by-direction similarity transform on u
    # 2. invert diagonal 
    # 3. transform back (direction-by-direction)

    # K - run-time symbolic
    knl = lp.make_kernel(ctx.devices[0],
            "[K] -> {[i,ip,j,jp,k,kp,m,e]: 0<=i,j,k,m<%d AND 0<=m,ip,jp,kp<%d 0<=e<K}" %M %N
            [

                # interpolate u to integration nodes
                "[|i,jp,kp] <float32>  u0[i,jp,kp,e] = sum_float32(ip, I[i,ip]*u[ip,jp,kp,e])",
                "[|i,j ,kp] <float32>  u1[i,j,kp,e]  = sum_float32(jp, I[j,jp]*u0[i,jp,kp,e])",
                "[|i,j ,k ] <float32>  Iu[i,j,k,e]   = sum_float32(kp, I[k,kp]*u1[i,j,kp,e])",

                # differentiate u on integration nodes
                "[|i,j ,k ] <float32>  Iur[i,j,k,e]  = sum_float32( m, D[i,m]*Iu[m,j,k,e])",
                "[|i,j ,k ] <float32>  Ius[i,j,k,e]  = sum_float32( m, D[j,m]*Iu[i,m,k,e])",
                "[|i,j ,k ] <float32>  Iut[i,j,k,e]  = sum_float32( m, D[k,m]*Iu[i,j,m,e])",

                # interpolate v to integration nodes
                "[|i,jp,kp] <float32>  v0[i,jp,kp,e] = sum_float32(ip, I[i,ip]*v[ip,jp,kp,e])",
                "[|i,j ,kp] <float32>  v1[i,j,kp,e]  = sum_float32(jp, I[j,jp]*v0[i,jp,kp,e])",
                "[|i,j ,k ] <float32>  Iv[i,j,k,e]   = sum_float32(kp, I[k,kp]*v1[i,j,kp,e])",

                # differentiate v on integration nodes
                "[|i,j ,k ] <float32>  Ivr[i,j,k,e]  = sum_float32(ip, D[i,m]*Iv[m,j,k,e])",
                "[|i,j ,k ] <float32>  Ivs[i,j,k,e]  = sum_float32(jp, D[j,m]*Iv[i,m,k,e])",
                "[|i,j ,k ] <float32>  Ivt[i,j,k,e]  = sum_float32(kp, D[k,m]*Iv[i,j,m,e])",

                # interpolate w to integration nodes
                "[|i,jp,kp] <float32>  w0[i,jp,kp,e] = sum_float32(ip, I[i,ip]*w[ip,jp,kp,e])",
                "[|i,j ,kp] <float32>  w1[i,j,kp,e]  = sum_float32(jp, I[j,jp]*w0[i,jp,kp,e])",
                "[|i,j ,k ] <float32>  Iw[i,j,k,e]   = sum_float32(kp, I[k,kp]*w1[i,j,kp,e])",

                # differentiate w on integration nodes
                "[|i,j ,k ] <float32>  Iwr[i,j,k,e]  = sum_float32(ip, D[i,m]*Iw[m,j,k,e])",
                "[|i,j ,k ] <float32>  Iws[i,j,k,e]  = sum_float32(jp, D[j,m]*Iw[i,m,k,e])",
                "[|i,j ,k ] <float32>  Iwt[i,j,k,e]  = sum_float32(kp, D[k,m]*Iw[i,j,m,e])",

                # find velocity in (r,s,t) coordinates
                "<float32> Vr[i,j,k,e] = "
                "G[i,j,k,0,e]*Iu[i,j,k,e] + G[i,j,k,1,e]*Iv[i,j,k,e] + G[i,j,k,2,e]*Iw[i,j,k,e]",
                "<float32> Vs[i,j,k,e] = "
                "G[i,j,k,3,e]*Iu[i,j,k,e] + G[i,j,k,4,e]*Iv[i,j,k,e] + G[i,j,k,5,e]*Iw[i,j,k,e]",
                "<float32> Vt[i,j,k,e] = "
                "G[i,j,k,6,e]*Iu[i,j,k,e] + G[i,j,k,7,e]*Iv[i,j,k,e] + G[i,j,k,8,e]*Iw[i,j,k,e]",

                # form nonlinear term on integration nodes
                "<float32> Nu[i,j,k,e] = Vr[i,j,k,e]*Iur[i,j,k,e]+Vs[i,j,k,e]*Ius[i,j,k,e]+Vt[i,j,k,e]*Iut[i,j,k,e]",
                "<float32> Nv[i,j,k,e] = Vr[i,j,k,e]*Ivr[i,j,k,e]+Vs[i,j,k,e]*Ivs[i,j,k,e]+Vt[i,j,k,e]*Ivt[i,j,k,e]",
                "<float32> Nw[i,j,k,e] = Vr[i,j,k,e]*Iwr[i,j,k,e]+Vs[i,j,k,e]*Iws[i,j,k,e]+Vt[i,j,k,e]*Iwt[i,j,k,e]",

                # L2 project Nu back to Lagrange basis
                "[|ip,j,k]  <float32> Nu2[ip,j,k,e]   = sum_float32(i, V[ip,i]*Nu[i,j,k,e])",
                "[|ip,jp,k] <float32> Nu1[ip,jp,k,e]  = sum_float32(j, V[jp,j]*Nu2[ip,j,k,e])",
                "INu[ip,jp,kp,e] = sum_float32(k, V[kp,k]*Nu1[ip,jp,k,e])",

                # L2 project Nv back to Lagrange basis
                "[|ip,j,k]  <float32> Nv2[ip,j,k,e]   = sum_float32(i, V[ip,i]*Nv[i,j,k,e])",
                "[|ip,jp,k] <float32> Nv1[ip,jp,k,e]  = sum_float32(j, V[jp,j]*Nv2[ip,j,k,e])",
                "INv[ip,jp,kp,e] = sum_float32(k, V[kp,k]*Nv1[ip,jp,k,e])",

                # L2 project Nw back to Lagrange basis
                "[|ip,j,k]  <float32> Nw2[ip,j,k,e]   = sum_float32(i, V[ip,i]*Nw[i,j,k,e])",
                "[|ip,jp,k] <float32> Nw1[ip,jp,k,e]  = sum_float32(j, V[jp,j]*Nw2[ip,j,k,e])",
                "INw[ip,jp,kp,e] = sum_float32(k, V[kp,k]*Nw1[ip,jp,k,e])",
                ],
            [
            lp.ArrayArg("u",   dtype, shape=field_shape, order=order),
            lp.ArrayArg("v",   dtype, shape=field_shape, order=order),
            lp.ArrayArg("w",   dtype, shape=field_shape, order=order),
            lp.ArrayArg("INu",   dtype, shape=field_shape, order=order),
            lp.ArrayArg("INv",   dtype, shape=field_shape, order=order),
            lp.ArrayArg("INw",   dtype, shape=field_shape, order=order),
            lp.ArrayArg("D",   dtype, shape=(M,M),  order=order),
            lp.ArrayArg("I",   dtype, shape=(M, N), order=order),
            lp.ArrayArg("V",   dtype, shape=(N, M), order=order),
            lp.ScalarArg("K",  np.int32, approximately=1000),
            ],
            name="sem_advect", assumptions="K>=1")

    print knl
    1/0

    knl = lp.split_dimension(knl, "e", 16, outer_tag="g.0")#, slabs=(0, 1))

    knl = lp.tag_dimensions(knl, dict(i="l.0", j="l.1"))

    print knl
    #1/0

    kernel_gen = lp.generate_loop_schedules(knl)
    kernel_gen = lp.check_kernels(kernel_gen, dict(K=1000), kill_level_min=5)

    a = make_well_conditioned_dev_matrix(queue, n, dtype=dtype, order=order)
    b = make_well_conditioned_dev_matrix(queue, n, dtype=dtype, order=order)
    c = cl_array.empty_like(a)
    refsol = np.dot(a.get(), b.get())

    def launcher(kernel, gsize, lsize, check):
        evt = kernel(queue, gsize(), lsize(), a.data, b.data, c.data,
                g_times_l=True)

        if check:
            check_error(refsol, c.get())

        return evt

    lp.drive_timing_run(kernel_gen, queue, launcher, 2*n**3)




if __name__ == "__main__":
    # make sure that import failures get reported, instead of skipping the
    # tests.
    import pyopencl as cl

    import sys
    if len(sys.argv) > 1:
        exec(sys.argv[1])
    else:
        from py.test.cmdline import main
        main([__file__])