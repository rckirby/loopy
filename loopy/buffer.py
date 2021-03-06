from __future__ import division, absolute_import
from six.moves import range

__copyright__ = "Copyright (C) 2012-2015 Andreas Kloeckner"

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

from loopy.array_buffer_map import (ArrayToBufferMap, NoOpArrayToBufferMap,
        AccessDescriptor)
from loopy.symbolic import (get_dependencies,
        RuleAwareIdentityMapper, SubstitutionRuleMappingContext,
        SubstitutionMapper)
from pymbolic.mapper.substitutor import make_subst_func

from pymbolic import var


# {{{ replace array access

class ArrayAccessReplacer(RuleAwareIdentityMapper):
    def __init__(self, rule_mapping_context,
            var_name, within, array_base_map, buf_var):
        super(ArrayAccessReplacer, self).__init__(rule_mapping_context)

        self.within = within

        self.array_base_map = array_base_map

        self.var_name = var_name
        self.modified_insn_ids = set()

        self.buf_var = buf_var

    def map_variable(self, expr, expn_state):
        result = None
        if expr.name == self.var_name and self.within(
                expn_state.kernel,
                expn_state.instruction,
                expn_state.stack):
            result = self.map_array_access((), expn_state)

        if result is None:
            return super(ArrayAccessReplacer, self).map_variable(expr, expn_state)
        else:
            self.modified_insn_ids.add(expn_state.insn_id)
            return result

    def map_subscript(self, expr, expn_state):
        result = None
        if expr.aggregate.name == self.var_name and self.within(
                expn_state.kernel,
                expn_state.instruction,
                expn_state.stack):
            result = self.map_array_access(expr.index, expn_state)

        if result is None:
            return super(ArrayAccessReplacer, self).map_subscript(expr, expn_state)
        else:
            self.modified_insn_ids.add(expn_state.insn_id)
            return result

    def map_array_access(self, index, expn_state):
        accdesc = AccessDescriptor(
            identifier=None,
            storage_axis_exprs=index)

        if not self.array_base_map.is_access_descriptor_in_footprint(accdesc):
            return None

        abm = self.array_base_map

        index = expn_state.apply_arg_context(index)

        assert len(index) == len(abm.non1_storage_axis_flags)

        access_subscript = []
        for i in range(len(index)):
            if not abm.non1_storage_axis_flags[i]:
                continue

            ax_index = index[i]
            from loopy.isl_helpers import simplify_via_aff
            ax_index = simplify_via_aff(
                    ax_index - abm.storage_base_indices[i])

            access_subscript.append(ax_index)

        result = self.buf_var
        if access_subscript:
            result = result.index(tuple(access_subscript))

        # Can't possibly be nested, but recurse anyway to
        # make sure substitution rules referenced below here
        # do not get thrown away.
        self.rec(result, expn_state.copy(arg_context={}))

        return result

# }}}


def buffer_array(kernel, var_name, buffer_inames, init_expression=None,
        store_expression=None, within=None, default_tag="l.auto",
        temporary_is_local=None, fetch_bounding_box=False):
    """
    :arg init_expression: Either *None* (indicating the prior value of the buffered
        array should be read) or an expression optionally involving the
        variable 'base' (which references the associated location in the array
        being buffered).
    :arg store_expression: Either *None* or an expression involving
        variables 'base' and 'buffer' (without array indices).
    """

    # {{{ process arguments

    if isinstance(init_expression, str):
        from loopy.symbolic import parse
        init_expression = parse(init_expression)

    if isinstance(store_expression, str):
        from loopy.symbolic import parse
        store_expression = parse(store_expression)

    if isinstance(buffer_inames, str):
        buffer_inames = [s.strip()
                for s in buffer_inames.split(",") if s.strip()]

    for iname in buffer_inames:
        if iname not in kernel.all_inames():
            raise RuntimeError("sweep iname '%s' is not a known iname"
                    % iname)

    buffer_inames = list(buffer_inames)
    buffer_inames_set = frozenset(buffer_inames)

    from loopy.context_matching import parse_stack_match
    within = parse_stack_match(within)

    if var_name in kernel.arg_dict:
        var_descr = kernel.arg_dict[var_name]
    elif var_name in kernel.temporary_variables:
        var_descr = kernel.temporary_variables[var_name]
    else:
        raise ValueError("variable '%s' not found" % var_name)

    from loopy.kernel.data import ArrayBase
    if isinstance(var_descr, ArrayBase):
        var_shape = var_descr.shape
    else:
        var_shape = ()

    if temporary_is_local is None:
        import loopy as lp
        temporary_is_local = lp.auto

    # }}}

    var_name_gen = kernel.get_var_name_generator()
    within_inames = set()

    access_descriptors = []
    for insn in kernel.instructions:
        if not within(kernel, insn.id, ()):
            continue

        for assignee, index in insn.assignees_and_indices():
            if assignee == var_name:
                within_inames.update(
                        (get_dependencies(index) & kernel.all_inames())
                        - buffer_inames_set)
                access_descriptors.append(
                        AccessDescriptor(
                            identifier=insn.id,
                            storage_axis_exprs=index))

    # {{{ find fetch/store inames

    init_inames = []
    store_inames = []
    new_iname_to_tag = {}

    for i in range(len(var_shape)):
        init_iname = var_name_gen("%s_init_%d" % (var_name, i))
        store_iname = var_name_gen("%s_store_%d" % (var_name, i))

        new_iname_to_tag[init_iname] = default_tag
        new_iname_to_tag[store_iname] = default_tag

        init_inames.append(init_iname)
        store_inames.append(store_iname)

    # }}}

    # {{{ modify loop domain

    non1_init_inames = []
    non1_store_inames = []

    if var_shape:
        # {{{ find domain to be changed

        from loopy.kernel.tools import DomainChanger
        domch = DomainChanger(kernel, buffer_inames_set | within_inames)

        if domch.leaf_domain_index is not None:
            # If the sweep inames are at home in parent domains, then we'll add
            # fetches with loops over copies of these parent inames that will end
            # up being scheduled *within* loops over these parents.

            for iname in buffer_inames_set:
                if kernel.get_home_domain_index(iname) != domch.leaf_domain_index:
                    raise RuntimeError("buffer iname '%s' is not 'at home' in the "
                            "sweep's leaf domain" % iname)

        # }}}

        abm = ArrayToBufferMap(kernel, domch.domain, buffer_inames,
                access_descriptors, len(var_shape))

        for i in range(len(var_shape)):
            if abm.non1_storage_axis_flags[i]:
                non1_init_inames.append(init_inames[i])
                non1_store_inames.append(store_inames[i])
            else:
                del new_iname_to_tag[init_inames[i]]
                del new_iname_to_tag[store_inames[i]]

        new_domain = domch.domain
        new_domain = abm.augment_domain_with_sweep(
                    new_domain, non1_init_inames,
                    boxify_sweep=fetch_bounding_box)
        new_domain = abm.augment_domain_with_sweep(
                    new_domain, non1_store_inames,
                    boxify_sweep=fetch_bounding_box)
        new_kernel_domains = domch.get_domains_with(new_domain)
        del new_domain

    else:
        # leave kernel domains unchanged
        new_kernel_domains = kernel.domains

        abm = NoOpArrayToBufferMap()

    # }}}

    # {{{ set up temp variable

    import loopy as lp

    buf_var_name = var_name_gen(based_on=var_name+"_buf")

    new_temporary_variables = kernel.temporary_variables.copy()
    temp_var = lp.TemporaryVariable(
            name=buf_var_name,
            dtype=var_descr.dtype,
            base_indices=(0,)*len(abm.non1_storage_shape),
            shape=tuple(abm.non1_storage_shape),
            is_local=temporary_is_local)

    new_temporary_variables[buf_var_name] = temp_var

    # }}}

    new_insns = []

    buf_var = var(buf_var_name)

    # {{{ generate init instruction

    buf_var_init = buf_var
    if non1_init_inames:
        buf_var_init = buf_var_init.index(
                tuple(var(iname) for iname in non1_init_inames))

    init_base = var(var_name)

    init_subscript = []
    init_iname_idx = 0
    if var_shape:
        for i in range(len(var_shape)):
            ax_subscript = abm.storage_base_indices[i]
            if abm.non1_storage_axis_flags[i]:
                ax_subscript += var(non1_init_inames[init_iname_idx])
                init_iname_idx += 1
            init_subscript.append(ax_subscript)

    if init_subscript:
        init_base = init_base.index(tuple(init_subscript))

    if init_expression is None:
        init_expression = init_base
    else:
        init_expression = init_expression
        init_expression = SubstitutionMapper(
                make_subst_func({
                    "base": init_base,
                    }))(init_expression)

    init_insn_id = kernel.make_unique_instruction_id(based_on="init_"+var_name)
    from loopy.kernel.data import ExpressionInstruction
    init_instruction = ExpressionInstruction(id=init_insn_id,
                assignee=buf_var_init,
                expression=init_expression,
                forced_iname_deps=frozenset(within_inames),
                insn_deps=frozenset(),
                insn_deps_is_final=True,
                )

    # }}}

    rule_mapping_context = SubstitutionRuleMappingContext(
            kernel.substitutions, kernel.get_var_name_generator())
    aar = ArrayAccessReplacer(rule_mapping_context, var_name,
            within, abm, buf_var)
    kernel = rule_mapping_context.finish_kernel(aar.map_kernel(kernel))

    did_write = False
    for insn_id in aar.modified_insn_ids:
        insn = kernel.id_to_insn[insn_id]
        if any(assignee_name == buf_var_name
                for assignee_name, _ in insn.assignees_and_indices()):
            did_write = True

    # {{{ add init_insn_id to insn_deps

    new_insns = []

    def none_to_empty_set(s):
        if s is None:
            return frozenset()
        else:
            return s

    for insn in kernel.instructions:
        if insn.id in aar.modified_insn_ids:
            new_insns.append(
                    insn.copy(
                        insn_deps=(
                            none_to_empty_set(insn.insn_deps)
                            | frozenset([init_insn_id]))))
        else:
            new_insns.append(insn)

    # }}}

    # {{{ generate store instruction

    buf_var_store = buf_var
    if non1_store_inames:
        buf_var_store = buf_var_store.index(
                tuple(var(iname) for iname in non1_store_inames))

    store_subscript = []
    store_iname_idx = 0
    if var_shape:
        for i in range(len(var_shape)):
            ax_subscript = abm.storage_base_indices[i]
            if abm.non1_storage_axis_flags[i]:
                ax_subscript += var(non1_store_inames[store_iname_idx])
                store_iname_idx += 1
            store_subscript.append(ax_subscript)

    store_target = var(var_name)
    if store_subscript:
        store_target = store_target.index(tuple(store_subscript))

    if store_expression is None:
        store_expression = buf_var_store
    else:
        store_expression = SubstitutionMapper(
                make_subst_func({
                    "base": store_target,
                    "buffer": buf_var_store,
                    }))(store_expression)

    from loopy.kernel.data import ExpressionInstruction
    store_instruction = ExpressionInstruction(
                id=kernel.make_unique_instruction_id(based_on="store_"+var_name),
                insn_deps=frozenset(aar.modified_insn_ids),
                assignee=store_target,
                expression=store_expression,
                forced_iname_deps=frozenset(within_inames))

    # }}}

    new_insns.append(init_instruction)
    if did_write:
        new_insns.append(store_instruction)

    kernel = kernel.copy(
            domains=new_kernel_domains,
            instructions=new_insns,
            temporary_variables=new_temporary_variables)

    from loopy import tag_inames
    kernel = tag_inames(kernel, new_iname_to_tag)

    return kernel

# vim: foldmethod=marker
