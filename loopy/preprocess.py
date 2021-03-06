from __future__ import division, absolute_import

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


import six
import numpy as np
from loopy.diagnostic import (
        LoopyError, WriteRaceConditionWarning, warn,
        LoopyAdvisory, DependencyTypeInferenceFailure)

from pytools.persistent_dict import PersistentDict
from loopy.tools import LoopyKeyBuilder
from loopy.version import DATA_MODEL_VERSION

import logging
logger = logging.getLogger(__name__)


# {{{ prepare for caching

def prepare_for_caching(kernel):
    import loopy as lp
    new_args = []

    for arg in kernel.args:
        dtype = arg.picklable_dtype
        if dtype is not None and dtype is not lp.auto:
            dtype = dtype.with_target(kernel.target)

        new_args.append(arg.copy(dtype=dtype))

    new_temporary_variables = {}
    for name, temp in six.iteritems(kernel.temporary_variables):
        dtype = temp.picklable_dtype
        if dtype is not None and dtype is not lp.auto:
            dtype = dtype.with_target(kernel.target)

        new_temporary_variables[name] = temp.copy(dtype=dtype)

    kernel = kernel.copy(
            args=new_args,
            temporary_variables=new_temporary_variables)

    return kernel

# }}}


# {{{ infer types

def _infer_var_type(kernel, var_name, type_inf_mapper, subst_expander):
    if var_name in kernel.all_params():
        return kernel.index_dtype, []

    def debug(s):
        logger.debug("%s: %s" % (kernel.name, s))

    dtypes = []

    import loopy as lp

    symbols_with_unavailable_types = []

    from loopy.diagnostic import DependencyTypeInferenceFailure
    for writer_insn_id in kernel.writer_map().get(var_name, []):
        writer_insn = kernel.id_to_insn[writer_insn_id]
        if not isinstance(writer_insn, lp.ExpressionInstruction):
            continue

        expr = subst_expander(writer_insn.expression)

        try:
            debug("             via expr %s" % expr)
            result = type_inf_mapper(expr)

            debug("             result: %s" % result)

            dtypes.append(result)

        except DependencyTypeInferenceFailure as e:
            debug("             failed: %s" % e)
            symbols_with_unavailable_types.append(e.symbol)

    if not dtypes:
        return None, symbols_with_unavailable_types

    result = type_inf_mapper.combine(dtypes)

    return result, []


class _DictUnionView:
    def __init__(self, children):
        self.children = children

    def get(self, key):
        try:
            return self[key]
        except KeyError:
            return None

    def __getitem__(self, key):
        for ch in self.children:
            try:
                return ch[key]
            except KeyError:
                pass

        raise KeyError(key)


def infer_unknown_types(kernel, expect_completion=False):
    """Infer types on temporaries and arguments."""

    logger.debug("%s: infer types" % kernel.name)

    def debug(s):
        logger.debug("%s: %s" % (kernel.name, s))

    unexpanded_kernel = kernel
    if kernel.substitutions:
        from loopy.subst import expand_subst
        kernel = expand_subst(kernel)

    new_temp_vars = kernel.temporary_variables.copy()
    new_arg_dict = kernel.arg_dict.copy()

    # {{{ fill queue

    # queue contains temporary variables
    queue = []

    import loopy as lp
    for tv in six.itervalues(kernel.temporary_variables):
        if tv.dtype is lp.auto:
            queue.append(tv)

    for arg in kernel.args:
        if arg.dtype is None:
            queue.append(arg)

    # }}}

    from loopy.expression import TypeInferenceMapper
    type_inf_mapper = TypeInferenceMapper(kernel,
            _DictUnionView([
                new_temp_vars,
                new_arg_dict
                ]))

    from loopy.symbolic import SubstitutionRuleExpander
    subst_expander = SubstitutionRuleExpander(kernel.substitutions)

    # {{{ work on type inference queue

    from loopy.kernel.data import TemporaryVariable, KernelArgument

    failed_names = set()
    while queue:
        item = queue.pop(0)

        debug("inferring type for %s %s" % (type(item).__name__, item.name))

        result, symbols_with_unavailable_types = \
                _infer_var_type(kernel, item.name, type_inf_mapper, subst_expander)

        failed = result is None
        if not failed:
            debug("     success: %s" % result)
            if isinstance(item, TemporaryVariable):
                new_temp_vars[item.name] = item.copy(dtype=result)
            elif isinstance(item, KernelArgument):
                new_arg_dict[item.name] = item.copy(dtype=result)
            else:
                raise LoopyError("unexpected item type in type inference")
        else:
            debug("     failure")

        if failed:
            if item.name in failed_names:
                # this item has failed before, give up.
                advice = ""
                if symbols_with_unavailable_types:
                    advice += (
                            " (need type of '%s'--check for missing arguments)"
                            % ", ".join(symbols_with_unavailable_types))

                if expect_completion:
                    raise LoopyError(
                            "could not determine type of '%s'%s"
                            % (item.name, advice))

                else:
                    # We're done here.
                    break

            # remember that this item failed
            failed_names.add(item.name)

            queue_names = set(qi.name for qi in queue)

            if queue_names == failed_names:
                # We did what we could...
                print(queue_names, failed_names, item.name)
                assert not expect_completion
                break

            # can't infer type yet, put back into queue
            queue.append(item)
        else:
            # we've made progress, reset failure markers
            failed_names = set()

    # }}}

    return unexpanded_kernel.copy(
            temporary_variables=new_temp_vars,
            args=[new_arg_dict[arg.name] for arg in kernel.args],
            )

# }}}


# {{{ decide which temporaries are local

def mark_local_temporaries(kernel):
    logger.debug("%s: mark local temporaries" % kernel.name)

    new_temp_vars = {}
    from loopy.kernel.data import LocalIndexTagBase
    import loopy as lp

    writers = kernel.writer_map()

    from loopy.symbolic import get_dependencies

    for temp_var in six.itervalues(kernel.temporary_variables):
        # Only fill out for variables that do not yet know if they're
        # local. (I.e. those generated by implicit temporary generation.)

        if temp_var.is_local is not lp.auto:
            new_temp_vars[temp_var.name] = temp_var
            continue

        my_writers = writers.get(temp_var.name, [])

        wants_to_be_local_per_insn = []
        for insn_id in my_writers:
            insn = kernel.id_to_insn[insn_id]

            # A write race will emerge if:
            #
            # - the variable is local
            #   and
            # - the instruction is run across more inames (locally) parallel
            #   than are reflected in the assignee indices.

            locparallel_compute_inames = set(iname
                    for iname in kernel.insn_inames(insn_id)
                    if isinstance(kernel.iname_to_tag.get(iname), LocalIndexTagBase))

            locparallel_assignee_inames = set(iname
                    for _, assignee_indices in insn.assignees_and_indices()
                    for iname in get_dependencies(assignee_indices)
                        & kernel.all_inames()
                    if isinstance(kernel.iname_to_tag.get(iname), LocalIndexTagBase))

            assert locparallel_assignee_inames <= locparallel_compute_inames

            if (locparallel_assignee_inames != locparallel_compute_inames
                    and bool(locparallel_assignee_inames)):
                warn(kernel, "write_race_local(%s)" % insn_id,
                        "instruction '%s' looks invalid: "
                        "it assigns to indices based on local IDs, but "
                        "its temporary '%s' cannot be made local because "
                        "a write race across the iname(s) '%s' would emerge. "
                        "(Do you need to add an extra iname to your prefetch?)"
                        % (insn_id, temp_var.name, ", ".join(
                            locparallel_compute_inames
                            - locparallel_assignee_inames)),
                        WriteRaceConditionWarning)

            wants_to_be_local_per_insn.append(
                    locparallel_assignee_inames == locparallel_compute_inames

                    # doesn't want to be local if there aren't any
                    # parallel inames:
                    and bool(locparallel_compute_inames))

        if not wants_to_be_local_per_insn:
            warn(kernel, "temp_to_write(%s)" % temp_var.name,
                    "temporary variable '%s' never written, eliminating"
                    % temp_var.name, LoopyAdvisory)

            continue

        is_local = any(wants_to_be_local_per_insn)

        from pytools import all
        if not all(wtbl == is_local for wtbl in wants_to_be_local_per_insn):
            raise LoopyError("not all instructions agree on whether "
                    "temporary '%s' should be in local memory" % temp_var.name)

        new_temp_vars[temp_var.name] = temp_var.copy(is_local=is_local)

    return kernel.copy(temporary_variables=new_temp_vars)

# }}}


# {{{ default dependencies

def add_default_dependencies(kernel):
    logger.debug("%s: default deps" % kernel.name)

    writer_map = kernel.writer_map()

    arg_names = set(arg.name for arg in kernel.args)

    var_names = arg_names | set(six.iterkeys(kernel.temporary_variables))

    dep_map = dict(
            (insn.id, insn.read_dependency_names() & var_names)
            for insn in kernel.instructions)

    new_insns = []
    for insn in kernel.instructions:
        if not insn.insn_deps_is_final:
            auto_deps = set()

            # {{{ add automatic dependencies

            all_my_var_writers = set()
            for var in dep_map[insn.id]:
                var_writers = writer_map.get(var, set())
                all_my_var_writers |= var_writers

                if not var_writers and var not in arg_names:
                    warn(kernel, "read_no_write(%s)" % var,
                            "temporary variable '%s' is read, but never written."
                            % var)

                if len(var_writers) == 1:
                    auto_deps.update(var_writers - set([insn.id]))

            # }}}

            insn_deps = insn.insn_deps
            if insn_deps is None:
                insn_deps = frozenset()

            insn = insn.copy(insn_deps=frozenset(auto_deps) | insn_deps)

        new_insns.append(insn)

    return kernel.copy(instructions=new_insns)

# }}}


# {{{ rewrite reduction to imperative form

def realize_reduction(kernel, insn_id_filter=None):
    """Rewrites reductions into their imperative form. With *insn_id_filter*
    specified, operate only on the instruction with an instruction id matching
    *insn_id_filter*.

    If *insn_id_filter* is given, only the outermost level of reductions will be
    expanded, inner reductions will be left alone (because they end up in a new
    instruction with a different ID, which doesn't match the filter).

    If *insn_id_filter* is not given, all reductions in all instructions will
    be realized.
    """

    logger.debug("%s: realize reduction" % kernel.name)

    new_insns = []

    var_name_gen = kernel.get_var_name_generator()
    new_temporary_variables = kernel.temporary_variables.copy()

    from loopy.expression import TypeInferenceMapper
    type_inf_mapper = TypeInferenceMapper(kernel)

    def map_reduction(expr, rec):
        # Only expand one level of reduction at a time, going from outermost to
        # innermost. Otherwise we get the (iname + insn) dependencies wrong.

        from pymbolic import var

        target_var_name = var_name_gen("acc_"+"_".join(expr.inames))
        target_var = var(target_var_name)

        try:
            arg_dtype = type_inf_mapper(expr.expr)
        except DependencyTypeInferenceFailure:
            raise LoopyError("failed to determine type of accumulator for "
                    "reduction '%s'" % expr)

        from loopy.kernel.data import ExpressionInstruction, TemporaryVariable

        new_temporary_variables[target_var_name] = TemporaryVariable(
                name=target_var_name,
                shape=(),
                dtype=expr.operation.result_dtype(
                    kernel.target, arg_dtype, expr.inames),
                is_local=False)

        outer_insn_inames = temp_kernel.insn_inames(insn)
        bad_inames = frozenset(expr.inames) & outer_insn_inames
        if bad_inames:
            raise LoopyError("reduction used within loop(s) that it was "
                    "supposed to reduce over: " + ", ".join(bad_inames))

        init_id = temp_kernel.make_unique_instruction_id(
                based_on="%s_%s_init" % (insn.id, "_".join(expr.inames)),
                extra_used_ids=set(i.id for i in generated_insns))

        init_insn = ExpressionInstruction(
                id=init_id,
                assignee=target_var,
                forced_iname_deps=outer_insn_inames - frozenset(expr.inames),
                insn_deps=frozenset(),
                expression=expr.operation.neutral_element(arg_dtype, expr.inames))

        generated_insns.append(init_insn)

        update_id = temp_kernel.make_unique_instruction_id(
                based_on="%s_%s_update" % (insn.id, "_".join(expr.inames)),
                extra_used_ids=set(i.id for i in generated_insns))

        reduction_insn = ExpressionInstruction(
                id=update_id,
                assignee=target_var,
                expression=expr.operation(
                    arg_dtype, target_var, expr.expr, expr.inames),
                insn_deps=frozenset([init_insn.id]) | insn.insn_deps,
                forced_iname_deps=temp_kernel.insn_inames(insn) | set(expr.inames))

        generated_insns.append(reduction_insn)

        new_insn_insn_deps.add(reduction_insn.id)

        return target_var

    from loopy.symbolic import ReductionCallbackMapper
    cb_mapper = ReductionCallbackMapper(map_reduction)

    insn_queue = kernel.instructions[:]

    temp_kernel = kernel

    import loopy as lp
    while insn_queue:
        new_insn_insn_deps = set()
        generated_insns = []

        insn = insn_queue.pop(0)

        if insn_id_filter is not None and insn.id != insn_id_filter \
                or not isinstance(insn, lp.ExpressionInstruction):
            new_insns.append(insn)
            continue

        # Run reduction expansion.
        new_expression = cb_mapper(insn.expression)

        if generated_insns:
            # An expansion happened, so insert the generated stuff plus
            # ourselves back into the queue.

            insn = insn.copy(
                        expression=new_expression,
                        insn_deps=insn.insn_deps
                        | frozenset(new_insn_insn_deps),
                        forced_iname_deps=temp_kernel.insn_inames(insn))

            insn_queue = generated_insns + [insn] + insn_queue

            # The reduction expander needs an up-to-date kernel
            # object to find dependencies. Keep temp_kernel up-to-date.

            temp_kernel = kernel.copy(
                    instructions=new_insns + insn_queue,
                    temporary_variables=new_temporary_variables)

        else:
            # nothing happened, we're done with insn
            assert not new_insn_insn_deps

            new_insns.append(insn)

    return kernel.copy(
            instructions=new_insns,
            temporary_variables=new_temporary_variables)

# }}}


# {{{ duplicate private vars for ilp and vec

from loopy.symbolic import IdentityMapper


class ExtraInameIndexInserter(IdentityMapper):
    def __init__(self, var_to_new_inames):
        self.var_to_new_inames = var_to_new_inames

    def map_subscript(self, expr):
        try:
            new_idx = self.var_to_new_inames[expr.aggregate.name]
        except KeyError:
            return IdentityMapper.map_subscript(self, expr)
        else:
            index = expr.index
            if not isinstance(index, tuple):
                index = (index,)
            index = tuple(self.rec(i) for i in index)

            return expr.aggregate.index(index + new_idx)

    def map_variable(self, expr):
        try:
            new_idx = self.var_to_new_inames[expr.name]
        except KeyError:
            return expr
        else:
            return expr.index(new_idx)


def duplicate_private_temporaries_for_ilp_and_vec(kernel):
    logger.debug("%s: duplicate temporaries for ilp" % kernel.name)

    wmap = kernel.writer_map()

    from loopy.kernel.data import IlpBaseTag, VectorizeTag

    var_to_new_ilp_inames = {}

    # {{{ find variables that need extra indices

    for tv in six.itervalues(kernel.temporary_variables):
        for writer_insn_id in wmap.get(tv.name, []):
            writer_insn = kernel.id_to_insn[writer_insn_id]
            ilp_inames = frozenset(iname
                    for iname in kernel.insn_inames(writer_insn)
                    if isinstance(
                        kernel.iname_to_tag.get(iname),
                        (IlpBaseTag, VectorizeTag)))

            referenced_ilp_inames = (ilp_inames
                    & writer_insn.write_dependency_names())

            new_ilp_inames = ilp_inames - referenced_ilp_inames

            if not new_ilp_inames:
                break

            if tv.name in var_to_new_ilp_inames:
                if new_ilp_inames != set(var_to_new_ilp_inames[tv.name]):
                    raise LoopyError("instruction '%s' requires adding "
                            "indices for ILP inames '%s' on var '%s', but previous "
                            "instructions required inames '%s'"
                            % (writer_insn_id, ", ".join(new_ilp_inames),
                                ", ".join(var_to_new_ilp_inames[tv.name])))

                continue

            var_to_new_ilp_inames[tv.name] = set(new_ilp_inames)

    # }}}

    # {{{ find ilp iname lengths

    from loopy.isl_helpers import static_max_of_pw_aff
    from loopy.symbolic import pw_aff_to_expr

    ilp_iname_to_length = {}
    for ilp_inames in six.itervalues(var_to_new_ilp_inames):
        for iname in ilp_inames:
            if iname in ilp_iname_to_length:
                continue

            bounds = kernel.get_iname_bounds(iname, constants_only=True)
            ilp_iname_to_length[iname] = int(pw_aff_to_expr(
                        static_max_of_pw_aff(bounds.size, constants_only=True)))

            assert static_max_of_pw_aff(
                    bounds.lower_bound_pw_aff, constants_only=True).plain_is_zero()

    # }}}

    # {{{ change temporary variables

    new_temp_vars = kernel.temporary_variables.copy()
    for tv_name, inames in six.iteritems(var_to_new_ilp_inames):
        tv = new_temp_vars[tv_name]
        extra_shape = tuple(ilp_iname_to_length[iname] for iname in inames)

        shape = tv.shape
        if shape is None:
            shape = ()

        dim_tags = ["c"] * (len(shape) + len(extra_shape))
        for i, iname in enumerate(inames):
            if isinstance(kernel.iname_to_tag.get(iname), VectorizeTag):
                dim_tags[len(shape) + i] = "vec"

        new_temp_vars[tv.name] = tv.copy(shape=shape + extra_shape,
                # Forget what you knew about data layout,
                # create from scratch.
                dim_tags=dim_tags)

    # }}}

    from pymbolic import var
    eiii = ExtraInameIndexInserter(
            dict((var_name, tuple(var(iname) for iname in inames))
                for var_name, inames in six.iteritems(var_to_new_ilp_inames)))

    new_insns = [
            insn.with_transformed_expressions(eiii)
            for insn in kernel.instructions]

    return kernel.copy(
        temporary_variables=new_temp_vars,
        instructions=new_insns)

# }}}


# {{{ find boostability of instructions

def find_boostability(kernel):
    logger.debug("%s: boostability" % kernel.name)

    writer_map = kernel.writer_map()

    arg_names = set(arg.name for arg in kernel.args)

    var_names = arg_names | set(six.iterkeys(kernel.temporary_variables))

    dep_map = dict(
            (insn.id, insn.read_dependency_names() & var_names)
            for insn in kernel.instructions)

    non_boostable_vars = set()

    new_insns = []
    for insn in kernel.instructions:
        all_my_var_writers = set()
        for var in dep_map[insn.id]:
            var_writers = writer_map.get(var, set())
            all_my_var_writers |= var_writers

        # {{{ find dependency loops, flag boostability

        while True:
            last_all_my_var_writers = all_my_var_writers

            for writer_insn_id in last_all_my_var_writers:
                for var in dep_map[writer_insn_id]:
                    all_my_var_writers = \
                            all_my_var_writers | writer_map.get(var, set())

            if last_all_my_var_writers == all_my_var_writers:
                break

        # }}}

        boostable = insn.id not in all_my_var_writers

        if not boostable:
            non_boostable_vars.update(
                    var_name for var_name, _ in insn.assignees_and_indices())

        insn = insn.copy(boostable=boostable)

        new_insns.append(insn)

    # {{{ remove boostability from isns that access non-boostable vars

    new2_insns = []
    for insn in new_insns:
        accessed_vars = insn.dependency_names()
        boostable = insn.boostable and not bool(non_boostable_vars & accessed_vars)
        new2_insns.append(insn.copy(boostable=boostable))

    # }}}

    return kernel.copy(instructions=new2_insns)

# }}}


# {{{ limit boostability

def limit_boostability(kernel):
    """Finds out which other inames an instruction's inames occur with
    and then limits boostability to just those inames.
    """

    logger.debug("%s: limit boostability" % kernel.name)

    iname_occurs_with = {}
    for insn in kernel.instructions:
        insn_inames = kernel.insn_inames(insn)
        for iname in insn_inames:
            iname_occurs_with.setdefault(iname, set()).update(insn_inames)

    iname_use_counts = {}
    for insn in kernel.instructions:
        for iname in kernel.insn_inames(insn):
            iname_use_counts[iname] = iname_use_counts.get(iname, 0) + 1

    single_use_inames = set(iname for iname, uc in six.iteritems(iname_use_counts)
            if uc == 1)

    new_insns = []
    for insn in kernel.instructions:
        if insn.boostable is None:
            raise LoopyError("insn '%s' has undetermined boostability" % insn.id)
        elif insn.boostable:
            boostable_into = set()
            for iname in kernel.insn_inames(insn):
                boostable_into.update(iname_occurs_with[iname])

            boostable_into -= kernel.insn_inames(insn) | single_use_inames

            # Even if boostable_into is empty, leave boostable flag on--it is used
            # for boosting into unused hw axes.

            insn = insn.copy(boostable_into=boostable_into)
        else:
            insn = insn.copy(boostable_into=set())

        new_insns.append(insn)

    return kernel.copy(instructions=new_insns)

# }}}


# {{{ rank inames by stride

def get_auto_axis_iname_ranking_by_stride(kernel, insn):
    from loopy.kernel.data import ImageArg, ValueArg

    approximate_arg_values = {}
    for arg in kernel.args:
        if isinstance(arg, ValueArg):
            if arg.approximately is not None:
                approximate_arg_values[arg.name] = arg.approximately
            else:
                raise LoopyError("No approximate arg value specified for '%s'"
                        % arg.name)

    # {{{ find all array accesses in insn

    from loopy.symbolic import ArrayAccessFinder
    ary_acc_exprs = list(ArrayAccessFinder()(insn.expression))

    from pymbolic.primitives import Subscript

    if isinstance(insn.assignee, Subscript):
        ary_acc_exprs.append(insn.assignee)

    # }}}

    # {{{ filter array accesses to only the global ones

    global_ary_acc_exprs = []

    for aae in ary_acc_exprs:
        ary_name = aae.aggregate.name
        arg = kernel.arg_dict.get(ary_name)
        if arg is None:
            continue

        if isinstance(arg, ImageArg):
            continue

        global_ary_acc_exprs.append(aae)

    # }}}

    # {{{ figure out automatic-axis inames

    from loopy.kernel.data import AutoLocalIndexTagBase
    auto_axis_inames = set(
            iname
            for iname in kernel.insn_inames(insn)
            if isinstance(kernel.iname_to_tag.get(iname),
                AutoLocalIndexTagBase))

    # }}}

    # {{{ figure out which iname should get mapped to local axis 0

    # maps inames to "aggregate stride"
    aggregate_strides = {}

    from loopy.symbolic import CoefficientCollector
    from pymbolic.primitives import Variable

    for aae in global_ary_acc_exprs:
        index_expr = aae.index
        if not isinstance(index_expr, tuple):
            index_expr = (index_expr,)

        ary_name = aae.aggregate.name
        arg = kernel.arg_dict.get(ary_name)

        if arg.dim_tags is None:
            from warnings import warn
            warn("Strides for '%s' are not known. Local axis assignment "
                    "is likely suboptimal." % arg.name)
            ary_strides = [1] * len(index_expr)
        else:
            ary_strides = []
            from loopy.kernel.array import FixedStrideArrayDimTag
            for dim_tag in arg.dim_tags:
                if isinstance(dim_tag, FixedStrideArrayDimTag):
                    ary_strides.append(dim_tag.stride)

        # {{{ construct iname_to_stride_expr

        iname_to_stride_expr = {}
        for iexpr_i, stride in zip(index_expr, ary_strides):
            if stride is None:
                continue
            coeffs = CoefficientCollector()(iexpr_i)
            for var, coeff in six.iteritems(coeffs):
                if (isinstance(var, Variable)
                        and var.name in auto_axis_inames):
                    # excludes '1', i.e.  the constant
                    new_stride = coeff*stride
                    old_stride = iname_to_stride_expr.get(var.name, None)
                    if old_stride is None or new_stride < old_stride:
                        iname_to_stride_expr[var.name] = new_stride

        # }}}

        from pymbolic import evaluate
        for iname, stride_expr in six.iteritems(iname_to_stride_expr):
            stride = evaluate(stride_expr, approximate_arg_values)
            aggregate_strides[iname] = aggregate_strides.get(iname, 0) + stride

    if aggregate_strides:
        very_large_stride = np.iinfo(np.int32).max

        return sorted((iname for iname in kernel.insn_inames(insn)),
                key=lambda iname: aggregate_strides.get(iname, very_large_stride))
    else:
        return None

    # }}}

# }}}


# {{{ assign automatic axes

def assign_automatic_axes(kernel, axis=0, local_size=None):
    logger.debug("%s: assign automatic axes" % kernel.name)

    from loopy.kernel.data import (AutoLocalIndexTagBase, LocalIndexTag)

    # Realize that at this point in time, axis lengths are already
    # fixed. So we compute them once and pass them to our recursive
    # copies.

    if local_size is None:
        _, local_size = kernel.get_grid_sizes_as_exprs(
                ignore_auto=True)

    # {{{ axis assignment helper function

    def assign_axis(recursion_axis, iname, axis=None):
        """Assign iname to local axis *axis* and start over by calling
        the surrounding function assign_automatic_axes.

        If *axis* is None, find a suitable axis automatically.
        """
        desired_length = kernel.get_constant_iname_length(iname)

        if axis is None:
            # {{{ find a suitable axis

            shorter_possible_axes = []
            test_axis = 0
            while True:
                if test_axis >= len(local_size):
                    break
                if test_axis in assigned_local_axes:
                    test_axis += 1
                    continue

                if local_size[test_axis] < desired_length:
                    shorter_possible_axes.append(test_axis)
                    test_axis += 1
                    continue
                else:
                    axis = test_axis
                    break

            # The loop above will find an unassigned local axis
            # that has enough 'room' for the iname. In the same traversal,
            # it also finds theoretically assignable axes that are shorter,
            # in the variable shorter_possible_axes.

            if axis is None and shorter_possible_axes:
                # sort as longest first
                shorter_possible_axes.sort(key=lambda ax: local_size[ax])
                axis = shorter_possible_axes[0]

            # }}}

        if axis is None:
            new_tag = None
        else:
            new_tag = LocalIndexTag(axis)
            if desired_length > local_size[axis]:
                from loopy import split_iname

                # Don't be tempted to switch the outer tag to unroll--this may
                # generate tons of code on some examples.

                return assign_automatic_axes(
                        split_iname(kernel, iname, inner_length=local_size[axis],
                            outer_tag=None, inner_tag=new_tag,
                            do_tagged_check=False),
                        axis=recursion_axis, local_size=local_size)

        if not isinstance(kernel.iname_to_tag.get(iname), AutoLocalIndexTagBase):
            raise LoopyError("trying to reassign '%s'" % iname)

        new_iname_to_tag = kernel.iname_to_tag.copy()
        new_iname_to_tag[iname] = new_tag
        return assign_automatic_axes(kernel.copy(iname_to_tag=new_iname_to_tag),
                axis=recursion_axis, local_size=local_size)

    # }}}

    # {{{ main assignment loop

    # assignment proceeds in one phase per axis, each time assigning the
    # smallest-stride available iname to the current axis

    import loopy as lp

    for insn in kernel.instructions:
        if not isinstance(insn, lp.ExpressionInstruction):
            continue

        auto_axis_inames = [
                iname
                for iname in kernel.insn_inames(insn)
                if isinstance(kernel.iname_to_tag.get(iname),
                    AutoLocalIndexTagBase)]

        if not auto_axis_inames:
            continue

        assigned_local_axes = set()

        for iname in kernel.insn_inames(insn):
            tag = kernel.iname_to_tag.get(iname)
            if isinstance(tag, LocalIndexTag):
                assigned_local_axes.add(tag.axis)

        if axis < len(local_size):
            # "valid" pass: try to assign a given axis

            if axis not in assigned_local_axes:
                iname_ranking = get_auto_axis_iname_ranking_by_stride(kernel, insn)
                if iname_ranking is not None:
                    for iname in iname_ranking:
                        prev_tag = kernel.iname_to_tag.get(iname)
                        if isinstance(prev_tag, AutoLocalIndexTagBase):
                            return assign_axis(axis, iname, axis)

        else:
            # "invalid" pass: There are still unassigned axis after the
            #  numbered "valid" passes--assign the remainder by length.

            # assign longest auto axis inames first
            auto_axis_inames.sort(key=kernel.get_constant_iname_length, reverse=True)

            if auto_axis_inames:
                return assign_axis(axis, auto_axis_inames.pop())

    # }}}

    # We've seen all instructions and not punted to recursion/restart because
    # of a new axis assignment.

    if axis >= len(local_size):
        return kernel
    else:
        return assign_automatic_axes(kernel, axis=axis+1,
                local_size=local_size)

# }}}


preprocess_cache = PersistentDict("loopy-preprocess-cache-v2-"+DATA_MODEL_VERSION,
        key_builder=LoopyKeyBuilder())


def preprocess_kernel(kernel, device=None):
    if device is not None:
        from warnings import warn
        warn("passing 'device' to preprocess_kernel() is deprecated",
                DeprecationWarning, stacklevel=2)

    from loopy.kernel import kernel_state
    if kernel.state != kernel_state.INITIAL:
        raise LoopyError("cannot re-preprocess an already preprocessed "
                "kernel")

    # {{{ cache retrieval

    from loopy import CACHING_ENABLED
    if CACHING_ENABLED:
        input_kernel = kernel

        try:
            result = preprocess_cache[kernel]
            logger.info("%s: preprocess cache hit" % kernel.name)
            return result
        except KeyError:
            pass

    # }}}

    logger.info("%s: preprocess start" % kernel.name)

    from loopy.subst import expand_subst
    kernel = expand_subst(kernel)

    # Ordering restriction:
    # Type inference doesn't handle substitutions. Get them out of the
    # way.

    kernel = infer_unknown_types(kernel, expect_completion=False)

    kernel = add_default_dependencies(kernel)

    # Ordering restrictions:
    #
    # - realize_reduction must happen after type inference because it needs
    #   to be able to determine the types of the reduced expressions.
    #
    # - realize_reduction must happen after default dependencies are added
    #   because it manipulates the insn_deps field, which could prevent
    #   defaults from being applied.

    kernel = realize_reduction(kernel)

    # Ordering restriction:
    # duplicate_private_temporaries_for_ilp because reduction accumulators
    # need to be duplicated by this.

    kernel = duplicate_private_temporaries_for_ilp_and_vec(kernel)
    kernel = mark_local_temporaries(kernel)
    kernel = assign_automatic_axes(kernel)
    kernel = find_boostability(kernel)
    kernel = limit_boostability(kernel)

    kernel = kernel.target.preprocess(kernel)

    logger.info("%s: preprocess done" % kernel.name)

    kernel = kernel.copy(
            state=kernel_state.PREPROCESSED)

    # {{{ prepare for caching

    # PicklableDtype instances for example need to know the target they're working
    # towards in order to pickle and unpickle them. This is the first pass that
    # uses caching, so we need to be ready to pickle. This means propagating
    # this target information.

    if CACHING_ENABLED:
        input_kernel = prepare_for_caching(input_kernel)

    kernel = prepare_for_caching(kernel)

    # }}}

    if CACHING_ENABLED:
        preprocess_cache[input_kernel] = kernel

    return kernel

# vim: foldmethod=marker
