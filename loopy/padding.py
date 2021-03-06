from __future__ import division
from __future__ import absolute_import
import six

__copyright__ = "Copyright (C) 2012 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""


from loopy.symbolic import RuleAwareIdentityMapper, SubstitutionRuleMappingContext


class ArgAxisSplitHelper(RuleAwareIdentityMapper):
    def __init__(self, rule_mapping_context, arg_names, handler):
        super(ArgAxisSplitHelper, self).__init__(rule_mapping_context)
        self.arg_names = arg_names
        self.handler = handler

    def map_subscript(self, expr, expn_state):
        if expr.aggregate.name in self.arg_names:
            return self.handler(expr)
        else:
            return super(ArgAxisSplitHelper, self).map_subscript(expr, expn_state)


def split_arg_axis(kernel, args_and_axes, count, auto_split_inames=True,
        split_kwargs=None):
    """
    :arg args_and_axes: a list of tuples *(arg, axis_nr)* indicating
        that the index in *axis_nr* should be split. The tuples may
        also be *(arg, axis_nr, "F")*, indicating that the index will
        be split as it would be according to Fortran order.

        If *args_and_axes* is a :class:`tuple`, it is automatically
        wrapped in a list, to make single splits easier.

    :arg count: The group size to use in the split.
    :arg auto_split_inames: Whether to automatically split inames
        encountered in the specified indices.
    :arg split_kwargs: arguments to pass to :func:`loopy.split_inames`

    Note that splits on the corresponding inames are carried out implicitly.
    The inames may *not* be split beforehand. (There's no *really* good reason
    for this--this routine is just not smart enough to deal with this.)
    """

    if count == 1:
        return kernel

    if split_kwargs is None:
        split_kwargs = {}

    # {{{ process input into arg_to_rest

    # where "rest" is the non-argument-name part of the input tuples
    # in args_and_axes
    def normalize_rest(rest):
        if len(rest) == 1:
            return (rest[0], "C")
        elif len(rest) == 2:
            return rest
        else:
            raise RuntimeError("split instruction '%s' not understood" % rest)

    if isinstance(args_and_axes, tuple):
        args_and_axes = [args_and_axes]

    arg_to_rest = dict((tup[0], normalize_rest(tup[1:])) for tup in args_and_axes)

    if len(args_and_axes) != len(arg_to_rest):
        raise RuntimeError("cannot split multiple axes of the same variable")

    del args_and_axes

    # }}}

    from loopy.kernel.data import GlobalArg
    for arg_name in arg_to_rest:
        if not isinstance(kernel.arg_dict[arg_name], GlobalArg):
            raise RuntimeError("only GlobalArg axes may be split")

    arg_to_idx = dict((arg.name, i) for i, arg in enumerate(kernel.args))

    # {{{ adjust args

    new_args = kernel.args[:]
    for arg_name, (axis, order) in six.iteritems(arg_to_rest):
        arg_idx = arg_to_idx[arg_name]

        arg = new_args[arg_idx]

        from pytools import div_ceil

        # {{{ adjust shape

        new_shape = arg.shape
        if new_shape is not None:
            new_shape = list(new_shape)
            axis_len = new_shape[axis]
            new_shape[axis] = count
            outer_len = div_ceil(axis_len, count)

            if order == "F":
                new_shape.insert(axis+1, outer_len)
            elif order == "C":
                new_shape.insert(axis, outer_len)
            else:
                raise RuntimeError("order '%s' not understood" % order)
            new_shape = tuple(new_shape)

        # }}}

        # {{{ adjust dim tags

        if arg.dim_tags is None:
            raise RuntimeError("dim_tags of '%s' are not known" % arg.name)
        new_dim_tags = list(arg.dim_tags)

        old_dim_tag = arg.dim_tags[axis]

        from loopy.kernel.array import FixedStrideArrayDimTag
        if not isinstance(old_dim_tag, FixedStrideArrayDimTag):
            raise RuntimeError("axis %d of '%s' is not tagged fixed-stride"
                    % (axis, arg.name))

        old_stride = old_dim_tag.stride
        outer_stride = count*old_stride

        if order == "F":
            new_dim_tags.insert(axis+1, FixedStrideArrayDimTag(outer_stride))
        elif order == "C":
            new_dim_tags.insert(axis, FixedStrideArrayDimTag(outer_stride))
        else:
            raise RuntimeError("order '%s' not understood" % order)

        new_dim_tags = tuple(new_dim_tags)

        # }}}

        new_args[arg_idx] = arg.copy(shape=new_shape, dim_tags=new_dim_tags)

    # }}}

    split_vars = {}

    var_name_gen = kernel.get_var_name_generator()

    def split_access_axis(expr):
        axis_nr, order = arg_to_rest[expr.aggregate.name]

        idx = expr.index
        if not isinstance(idx, tuple):
            idx = (idx,)
        idx = list(idx)

        axis_idx = idx[axis_nr]

        if auto_split_inames:
            from pymbolic.primitives import Variable
            if not isinstance(axis_idx, Variable):
                raise RuntimeError("found access '%s' in which axis %d is not a "
                        "single variable--cannot split "
                        "(Have you tried to do the split yourself, manually, "
                        "beforehand? If so, you shouldn't.)"
                        % (expr, axis_nr))

            split_iname = idx[axis_nr].name
            assert split_iname in kernel.all_inames()

            try:
                outer_iname, inner_iname = split_vars[split_iname]
            except KeyError:
                outer_iname = var_name_gen(split_iname+"_outer")
                inner_iname = var_name_gen(split_iname+"_inner")
                split_vars[split_iname] = outer_iname, inner_iname

            inner_index = Variable(inner_iname)
            outer_index = Variable(outer_iname)

        else:
            inner_index = axis_idx % count
            outer_index = axis_idx // count

        idx[axis_nr] = inner_index

        if order == "F":
            idx.insert(axis+1, outer_index)
        elif order == "C":
            idx.insert(axis, outer_index)
        else:
            raise RuntimeError("order '%s' not understood" % order)

        return expr.aggregate.index(tuple(idx))

    rule_mapping_context = SubstitutionRuleMappingContext(
            kernel.substitutions, var_name_gen)
    aash = ArgAxisSplitHelper(rule_mapping_context,
            set(six.iterkeys(arg_to_rest)), split_access_axis)
    kernel = rule_mapping_context.finish_kernel(aash.map_kernel(kernel))

    kernel = kernel.copy(args=new_args)

    if auto_split_inames:
        from loopy import split_iname
        for iname, (outer_iname, inner_iname) in six.iteritems(split_vars):
            kernel = split_iname(kernel, iname, count,
                    outer_iname=outer_iname, inner_iname=inner_iname,
                    **split_kwargs)

    return kernel


def find_padding_multiple(kernel, variable, axis, align_bytes, allowed_waste=0.1):
    arg = kernel.arg_dict[variable]

    if arg.dim_tags is None:
        raise RuntimeError("cannot find padding multiple--dim_tags of '%s' "
                "are not known" % variable)

    dim_tag = arg.dim_tags[axis]

    from loopy.kernel.array import FixedStrideArrayDimTag
    if not isinstance(dim_tag, FixedStrideArrayDimTag):
        raise RuntimeError("cannot find padding multiple--"
                "axis %d of '%s' is not tagged fixed-stride"
                % (axis, variable))

    stride = dim_tag.stride

    if not isinstance(stride, int):
        raise RuntimeError("cannot find padding multiple--stride is not a "
                "known integer")

    from pytools import div_ceil

    multiple = 1
    while True:
        true_size = multiple * stride
        padded_size = div_ceil(true_size, align_bytes) * align_bytes

        if (padded_size - true_size) / true_size <= allowed_waste:
            return multiple

        multiple += 1


def add_padding(kernel, variable, axis, align_bytes):
    arg_to_idx = dict((arg.name, i) for i, arg in enumerate(kernel.args))
    arg_idx = arg_to_idx[variable]

    new_args = kernel.args[:]
    arg = new_args[arg_idx]

    if arg.dim_tags is None:
        raise RuntimeError("cannot add padding--dim_tags of '%s' "
                "are not known" % variable)

    new_dim_tags = list(arg.dim_tags)
    dim_tag = new_dim_tags[axis]

    from loopy.kernel.array import FixedStrideArrayDimTag
    if not isinstance(dim_tag, FixedStrideArrayDimTag):
        raise RuntimeError("cannot find padding multiple--"
                "axis %d of '%s' is not tagged fixed-stride"
                % (axis, variable))

    stride = dim_tag.stride
    if not isinstance(stride, int):
        raise RuntimeError("cannot find split granularity--stride is not a "
                "known integer")

    from pytools import div_ceil
    new_dim_tags[axis] = FixedStrideArrayDimTag(
            div_ceil(stride, align_bytes) * align_bytes)

    new_args[arg_idx] = arg.copy(dim_tags=tuple(new_dim_tags))

    return kernel.copy(args=new_args)


# vim: foldmethod=marker
