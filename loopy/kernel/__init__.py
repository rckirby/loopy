"""Kernel object."""

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
from six.moves import range, zip

import numpy as np
from pytools import RecordWithoutPickling, Record, memoize_method
import islpy as isl
from islpy import dim_type
import re

from pytools import UniqueNameGenerator, generate_unique_names

from loopy.library.function import (
        default_function_mangler,
        single_arg_function_mangler)

from loopy.diagnostic import CannotBranchDomainTree


# {{{ unique var names

def _is_var_name_conflicting_with_longer(name_a, name_b):
    # Array dimensions implemented as separate arrays generate
    # names by appending '_s<NUMBER>'. Make sure that no
    # conflicts can arise from these names.

    # Only deal with the case of b longer than a.
    if not name_b.startswith(name_a):
        return False

    return re.match("^%s_s[0-9]+" % re.escape(name_b), name_a) is not None


def _is_var_name_conflicting(name_a, name_b):
    if name_a == name_b:
        return True

    return (
            _is_var_name_conflicting_with_longer(name_a, name_b)
            or _is_var_name_conflicting_with_longer(name_b, name_a))


class _UniqueVarNameGenerator(UniqueNameGenerator):
    def is_name_conflicting(self, name):
        from pytools import any
        return any(
                _is_var_name_conflicting(name, other_name)
                for other_name in self.existing_names)

# }}}


# {{{ loop kernel object

class kernel_state:  # noqa
    INITIAL = 0
    PREPROCESSED = 1
    SCHEDULED = 2


class LoopKernel(RecordWithoutPickling):
    """These correspond more or less directly to arguments of
    :func:`loopy.make_kernel`.

    .. attribute:: domains

        a list of :class:`islpy.BasicSet` instances

    .. attribute:: instructions
    .. attribute:: args

        A list of :class:`loopy.kernel.data.KernelArgument`

    .. attribute:: schedule

        *None* or a list of :class:`loopy.schedule.ScheduleItem`

    .. attribute:: name
    .. attribute:: preambles
    .. attribute:: preamble_generators
    .. attribute:: assumptions
    .. attribute:: local_sizes
    .. attribute:: temporary_variables

        A :class:`dict` of mapping variable names to
        :class:`loopy.kernel.data.TemporaryVariable`
        instances.

    .. attribute:: iname_to_tag

        A :class:`dict` mapping inames (as strings)
        to instances of :class:`loopy.kernel.data.IndexTag`.

    .. attribute:: function_manglers
    .. attribute:: symbol_manglers

    .. attribute:: substitutions

        a mapping from substitution names to
        :class:`SubstitutionRule` objects

    .. attribute:: iname_slab_increments

        a dictionary mapping inames to (lower_incr,
        upper_incr) tuples that will be separated out in the execution to generate
        'bulk' slabs with fewer conditionals.

    .. attribute:: loop_priority

        A list of inames. The earlier in the list the iname occurs, the earlier
        it will be scheduled. (This applies to inames with non-parallel
        implementation tags.)

    .. attribute:: silenced_warnings

    .. attribute:: applied_iname_rewrites

        A list of past substitution dictionaries that
        were applied to the kernel. These are stored so that they may be repeated
        on expressions the user specifies later.

    .. attribute:: cache_manager
    .. attribute:: options

        An instance of :class:`loopy.Options`

    .. attribute:: state

        A value from :class:`kernel_state`.

    .. attribute:: target

        A subclass of :class:`loopy.target.TargetBase`.
    """

    # {{{ constructor

    def __init__(self, domains, instructions, args=[], schedule=None,
            name="loopy_kernel",
            preambles=[],
            preamble_generators=[],
            assumptions=None,
            local_sizes={},
            temporary_variables={},
            iname_to_tag={},
            substitutions={},
            function_manglers=[
                default_function_mangler,
                single_arg_function_mangler,
                ],
            symbol_manglers=[],

            iname_slab_increments={},
            loop_priority=[],
            silenced_warnings=[],

            applied_iname_rewrites=[],
            cache_manager=None,
            index_dtype=np.int32,
            options=None,

            state=kernel_state.INITIAL,
            target=None,

            # When kernels get intersected in slab decomposition,
            # their grid sizes shouldn't change. This provides
            # a way to forward sub-kernel grid size requests.
            get_grid_sizes=None):

        if cache_manager is None:
            from loopy.kernel.tools import SetOperationCacheManager
            cache_manager = SetOperationCacheManager()

        # {{{ make instruction ids unique

        from loopy.kernel.creation import UniqueName

        insn_ids = set()
        for insn in instructions:
            if insn.id is not None and not isinstance(insn.id, UniqueName):
                if insn.id in insn_ids:
                    raise RuntimeError("duplicate instruction id: %s" % insn.id)
                insn_ids.add(insn.id)

        insn_id_gen = UniqueNameGenerator(insn_ids)

        new_instructions = []

        for insn in instructions:
            if insn.id is None:
                new_instructions.append(
                        insn.copy(id=insn_id_gen("insn")))
            elif isinstance(insn.id, UniqueName):
                new_instructions.append(
                        insn.copy(id=insn_id_gen(insn.id.name)))
            else:
                new_instructions.append(insn)

        instructions = new_instructions
        del new_instructions

        # }}}

        # {{{ process assumptions

        if assumptions is None:
            dom0_space = domains[0].get_space()
            assumptions_space = isl.Space.params_alloc(
                    dom0_space.get_ctx(), dom0_space.dim(dim_type.param))
            for i in range(dom0_space.dim(dim_type.param)):
                assumptions_space = assumptions_space.set_dim_name(
                        dim_type.param, i,
                        dom0_space.get_dim_name(dim_type.param, i))
            assumptions = isl.BasicSet.universe(assumptions_space)

        elif isinstance(assumptions, str):
            assumptions_set_str = "[%s] -> { : %s}" \
                    % (",".join(s for s in self.outer_params(domains)),
                        assumptions)
            assumptions = isl.BasicSet.read_from_str(domains[0].get_ctx(),
                    assumptions_set_str)

        assert assumptions.is_params()

        # }}}

        index_dtype = np.dtype(index_dtype)
        if index_dtype.kind != 'i':
            raise TypeError("index_dtype must be an integer")
        if np.iinfo(index_dtype).min >= 0:
            raise TypeError("index_dtype must be signed")

        if get_grid_sizes is not None:
            # overwrites method down below
            self.get_grid_sizes = get_grid_sizes

        if state not in [
                kernel_state.INITIAL,
                kernel_state.PREPROCESSED,
                kernel_state.SCHEDULED,
                ]:
            raise ValueError("invalid value for 'state'")

        assert all(dom.get_ctx() == isl.DEFAULT_CONTEXT for dom in domains)
        assert assumptions.get_ctx() == isl.DEFAULT_CONTEXT

        RecordWithoutPickling.__init__(self,
                domains=domains,
                instructions=instructions,
                args=args,
                schedule=schedule,
                name=name,
                preambles=preambles,
                preamble_generators=preamble_generators,
                assumptions=assumptions,
                iname_slab_increments=iname_slab_increments,
                loop_priority=loop_priority,
                silenced_warnings=silenced_warnings,
                temporary_variables=temporary_variables,
                local_sizes=local_sizes,
                iname_to_tag=iname_to_tag,
                substitutions=substitutions,
                cache_manager=cache_manager,
                applied_iname_rewrites=applied_iname_rewrites,
                function_manglers=function_manglers,
                symbol_manglers=symbol_manglers,
                index_dtype=index_dtype,
                options=options,
                state=state,
                target=target)

    # }}}

    # {{{ function mangling

    def mangle_function(self, identifier, arg_dtypes):
        manglers = self.target.function_manglers() + self.function_manglers

        for mangler in manglers:
            mangle_result = mangler(self.target, identifier, arg_dtypes)
            if mangle_result is not None:
                return mangle_result

        return None

    # }}}

    # {{{ symbol mangling

    def mangle_symbol(self, identifier):
        manglers = self.target.symbol_manglers() + self.symbol_manglers

        for mangler in manglers:
            result = mangler(self.target, identifier)
            if result is not None:
                return result

        return None

    # }}}

    # {{{ name wrangling

    @memoize_method
    def non_iname_variable_names(self):
        return (set(six.iterkeys(self.arg_dict))
                | set(six.iterkeys(self.temporary_variables)))

    @memoize_method
    def all_variable_names(self):
        return (
                set(six.iterkeys(self.temporary_variables))
                | set(six.iterkeys(self.substitutions))
                | set(arg.name for arg in self.args)
                | set(self.all_inames()))

    def get_var_name_generator(self):
        return _UniqueVarNameGenerator(self.all_variable_names())

    def make_unique_instruction_id(self, insns=None, based_on="insn",
            extra_used_ids=set()):
        if insns is None:
            insns = self.instructions

        used_ids = set(insn.id for insn in insns) | extra_used_ids

        for id_str in generate_unique_names(based_on):
            if id_str not in used_ids:
                return id_str

    def get_var_descriptor(self, name):
        try:
            return self.arg_dict[name]
        except KeyError:
            pass

        try:
            return self.temporary_variables[name]
        except KeyError:
            pass

        raise ValueError("nothing known about variable '%s'" % name)

    @property
    @memoize_method
    def id_to_insn(self):
        return dict((insn.id, insn) for insn in self.instructions)

    # }}}

    # {{{ domain wrangling

    @memoize_method
    def parents_per_domain(self):
        """Return a list corresponding to self.domains (by index)
        containing domain indices which are nested around this
        domain.

        Each domains nest list walks from the leaves of the nesting
        tree to the root.
        """

        # The stack of iname sets records which inames are active
        # as we step through the linear list of domains. It also
        # determines the granularity of inames to be popped/decactivated
        # if we ascend a level.

        iname_set_stack = []
        result = []

        from loopy.kernel.tools import is_domain_dependent_on_inames

        for dom_idx, dom in enumerate(self.domains):
            inames = set(dom.get_var_names(dim_type.set))

            # This next domain may be nested inside the previous domain.
            # Or it may not, in which case we need to figure out how many
            # levels of parents we need to discard in order to find the
            # true parent.

            discard_level_count = 0
            while discard_level_count < len(iname_set_stack):
                last_inames = iname_set_stack[-1-discard_level_count]

                if is_domain_dependent_on_inames(self, dom_idx, last_inames):
                    break

                discard_level_count += 1

            if discard_level_count:
                iname_set_stack = iname_set_stack[:-discard_level_count]

            if result:
                parent = len(result)-1
            else:
                parent = None

            for i in range(discard_level_count):
                assert parent is not None
                parent = result[parent]

            # found this domain's parent
            result.append(parent)

            if iname_set_stack:
                parent_inames = iname_set_stack[-1]
            else:
                parent_inames = set()
            iname_set_stack.append(parent_inames | inames)

        return result

    @memoize_method
    def all_parents_per_domain(self):
        """Return a list corresponding to self.domains (by index)
        containing domain indices which are nested around this
        domain.

        Each domains nest list walks from the leaves of the nesting
        tree to the root.
        """
        result = []

        ppd = self.parents_per_domain()
        for dom, parent in zip(self.domains, ppd):
            # keep walking up tree to find *all* parents
            dom_result = []
            while parent is not None:
                dom_result.insert(0, parent)
                parent = ppd[parent]

            result.append(dom_result)

        return result

    @memoize_method
    def _get_home_domain_map(self):
        return dict(
                (iname, i_domain)
                for i_domain, dom in enumerate(self.domains)
                for iname in dom.get_var_names(dim_type.set))

    def get_home_domain_index(self, iname):
        return self._get_home_domain_map()[iname]

    @property
    def isl_context(self):
        for dom in self.domains:
            return dom.get_ctx()

        assert False

    @memoize_method
    def combine_domains(self, domains):
        """
        :arg domains: domain indices of domains to be combined. More 'dominant'
            domains (those which get most say on the actual dim_type of an iname)
            must be later in the order.
        """
        assert isinstance(domains, tuple)  # for caching

        if not domains:
            return isl.BasicSet.universe(isl.Space.set_alloc(
                self.isl_context, 0, 0))

        result = None
        for dom_index in domains:
            dom = self.domains[dom_index]
            if result is None:
                result = dom
            else:
                aligned_dom, aligned_result = isl.align_two(
                        dom, result, across_dim_types=True)
                result = aligned_result & aligned_dom

        return result

    def get_inames_domain(self, inames):
        if not inames:
            return self.combine_domains(())

        if isinstance(inames, str):
            inames = frozenset([inames])
        if not isinstance(inames, frozenset):
            inames = frozenset(inames)

            from warnings import warn
            warn("get_inames_domain did not get a frozenset", stacklevel=2)

        return self._get_inames_domain_backend(inames)

    @memoize_method
    def get_leaf_domain_indices(self, inames):
        """Find the leaves of the domain tree needed to cover all inames.

        :arg inames: a non-mutable iterable
        """

        hdm = self._get_home_domain_map()
        ppd = self.all_parents_per_domain()

        domain_indices = set()

        # map root -> leaf
        root_to_leaf = {}

        for iname in inames:
            home_domain_index = hdm[iname]
            if home_domain_index in domain_indices:
                # nothin' new
                continue

            domain_parents = [home_domain_index] + ppd[home_domain_index]
            current_root = domain_parents[-1]
            previous_leaf = root_to_leaf.get(current_root)

            if previous_leaf is not None:
                # Check that we don't branch the domain tree.
                #
                # Branching the domain tree is dangerous/ill-formed because
                # it can introduce artificial restrictions on variables
                # further up the tree.

                prev_parents = set(ppd[previous_leaf])
                if not prev_parents <= set(domain_parents):
                    raise CannotBranchDomainTree("iname set '%s' requires "
                            "branch in domain tree (when adding '%s')"
                            % (", ".join(inames), iname))
            else:
                # We're adding a new root. That's fine.
                pass

            root_to_leaf[current_root] = home_domain_index
            domain_indices.update(domain_parents)

        return list(root_to_leaf.values())

    @memoize_method
    def _get_inames_domain_backend(self, inames):
        domain_indices = set()
        for leaf_dom_idx in self.get_leaf_domain_indices(inames):
            domain_indices.add(leaf_dom_idx)
            domain_indices.update(self.all_parents_per_domain()[leaf_dom_idx])

        return self.combine_domains(tuple(sorted(domain_indices)))

    # }}}

    # {{{ iname wrangling

    @memoize_method
    def all_inames(self):
        result = set()
        for dom in self.domains:
            result.update(dom.get_var_names(dim_type.set))
        return frozenset(result)

    @memoize_method
    def all_params(self):
        all_inames = self.all_inames()

        result = set()
        for dom in self.domains:
            result.update(set(dom.get_var_names(dim_type.param)) - all_inames)

        return frozenset(result)

    def outer_params(self, domains=None):
        if domains is None:
            domains = self.domains

        all_inames = set()
        all_params = set()
        for dom in domains:
            all_inames.update(dom.get_var_names(dim_type.set))
            all_params.update(dom.get_var_names(dim_type.param))

        return all_params-all_inames

    @memoize_method
    def all_insn_inames(self):
        """Return a mapping from instruction ids to inames inside which
        they should be run.
        """

        from loopy.kernel.tools import find_all_insn_inames
        return find_all_insn_inames(self)

    @memoize_method
    def all_referenced_inames(self):
        result = set()
        for inames in six.itervalues(self.all_insn_inames()):
            result.update(inames)
        return result

    def insn_inames(self, insn):
        from loopy.kernel.data import InstructionBase
        if isinstance(insn, InstructionBase):
            return self.all_insn_inames()[insn.id]
        else:
            return self.all_insn_inames()[insn]

    @memoize_method
    def iname_to_insns(self):
        result = dict(
                (iname, set()) for iname in self.all_inames())
        for insn in self.instructions:
            for iname in self.insn_inames(insn):
                result[iname].add(insn.id)

        return result

    @memoize_method
    def remove_inames_for_shared_hw_axes(self, cond_inames):
        """
        See if cond_inames contains references to two (or more) inames that
        boil down to the same tag. If so, exclude them. (We shouldn't be writing
        conditionals for such inames because we would be implicitly restricting
        the other inames as well.)
        """

        tag_key_uses = {}

        from loopy.kernel.data import HardwareParallelTag

        for iname in cond_inames:
            tag = self.iname_to_tag.get(iname)

            if isinstance(tag, HardwareParallelTag):
                tag_key_uses.setdefault(tag.key, []).append(iname)

        multi_use_keys = set(
                key for key, user_inames in six.iteritems(tag_key_uses)
                if len(user_inames) > 1)

        multi_use_inames = set()
        for iname in cond_inames:
            tag = self.iname_to_tag.get(iname)
            if isinstance(tag, HardwareParallelTag) and tag.key in multi_use_keys:
                multi_use_inames.add(iname)

        return frozenset(cond_inames - multi_use_inames)

    # }}}

    # {{{ dependency wrangling

    @memoize_method
    def recursive_insn_dep_map(self):
        """Returns a :class:`dict` mapping an instruction IDs *a*
        to all instruction IDs it directly or indirectly depends
        on.
        """

        result = {}

        def compute_deps(insn_id):
            try:
                return result[insn_id]
            except KeyError:
                pass

            insn = self.id_to_insn[insn_id]
            insn_result = set(insn.insn_deps)

            for dep in list(insn.insn_deps):
                insn_result.update(compute_deps(dep))

            result[insn_id] = frozenset(insn_result)
            return insn_result

        for insn in self.instructions:
            compute_deps(insn.id)

        return result

    # }}}

    # {{{ read and written variables

    @memoize_method
    def reader_map(self):
        """
        :return: a dict that maps variable names to ids of insns that read that
          variable.
        """
        result = {}

        admissible_vars = (
                set(arg.name for arg in self.args)
                | set(six.iterkeys(self.temporary_variables)))

        for insn in self.instructions:
            for var_name in insn.read_dependency_names() & admissible_vars:
                result.setdefault(var_name, set()).add(insn.id)

    @memoize_method
    def writer_map(self):
        """
        :return: a dict that maps variable names to ids of insns that write
            to that variable.
        """
        result = {}

        for insn in self.instructions:
            for var_name, _ in insn.assignees_and_indices():
                result.setdefault(var_name, set()).add(insn.id)

        return result

    @memoize_method
    def get_read_variables(self):
        result = set()
        for insn in self.instructions:
            result.update(insn.read_dependency_names())
        return result

    @memoize_method
    def get_written_variables(self):
        return frozenset(
                var_name
                for insn in self.instructions
                for var_name, _ in insn.assignees_and_indices())

    # }}}

    # {{{ argument wrangling

    @property
    @memoize_method
    def arg_dict(self):
        return dict((arg.name, arg) for arg in self.args)

    @property
    @memoize_method
    def scalar_loop_args(self):
        from loopy.kernel.data import ValueArg

        if self.args is None:
            return []
        else:
            from pytools import flatten
            loop_arg_names = list(flatten(dom.get_var_names(dim_type.param)
                    for dom in self.domains))
            return [arg.name for arg in self.args if isinstance(arg, ValueArg)
                    if arg.name in loop_arg_names]

    @memoize_method
    def global_var_names(self):
        from loopy.kernel.data import GlobalArg
        return set(arg.name for arg in self.args
            if isinstance(arg, GlobalArg))

    # }}}

    # {{{ bounds finding

    @memoize_method
    def get_iname_bounds(self, iname, constants_only=False):
        domain = self.get_inames_domain(frozenset([iname]))

        assumptions = self.assumptions.project_out_except(
                set(domain.get_var_dict(dim_type.param)), [dim_type.param])

        aligned_assumptions, domain = isl.align_two(assumptions, domain)

        dom_intersect_assumptions = aligned_assumptions & domain

        if constants_only:
            # Kill all variable dependencies
            dom_intersect_assumptions = dom_intersect_assumptions.project_out_except(
                    [iname], [dim_type.param, dim_type.set])

        iname_idx = dom_intersect_assumptions.get_var_dict()[iname][1]

        lower_bound_pw_aff = (
                self.cache_manager.dim_min(
                    dom_intersect_assumptions, iname_idx)
                .coalesce())
        upper_bound_pw_aff = (
                self.cache_manager.dim_max(
                    dom_intersect_assumptions, iname_idx)
                .coalesce())

        class BoundsRecord(Record):
            pass

        size = (upper_bound_pw_aff - lower_bound_pw_aff + 1)
        size = size.gist(assumptions)

        return BoundsRecord(
                lower_bound_pw_aff=lower_bound_pw_aff,
                upper_bound_pw_aff=upper_bound_pw_aff,
                size=size)

    @memoize_method
    def get_constant_iname_length(self, iname):
        from loopy.isl_helpers import static_max_of_pw_aff
        from loopy.symbolic import aff_to_expr
        return int(aff_to_expr(static_max_of_pw_aff(
                self.get_iname_bounds(iname, constants_only=True).size,
                constants_only=True)))

    @memoize_method
    def get_grid_sizes(self, ignore_auto=False):
        all_inames_by_insns = set()
        for insn in self.instructions:
            all_inames_by_insns |= self.insn_inames(insn)

        if not all_inames_by_insns <= self.all_inames():
            raise RuntimeError("some inames collected from instructions (%s) "
                    "are not present in domain (%s)"
                    % (", ".join(sorted(all_inames_by_insns)),
                        ", ".join(sorted(self.all_inames()))))

        global_sizes = {}
        local_sizes = {}

        from loopy.kernel.data import (
                GroupIndexTag, LocalIndexTag,
                AutoLocalIndexTagBase)

        for iname in self.all_inames():
            tag = self.iname_to_tag.get(iname)

            if isinstance(tag, GroupIndexTag):
                tgt_dict = global_sizes
            elif isinstance(tag, LocalIndexTag):
                tgt_dict = local_sizes
            elif isinstance(tag, AutoLocalIndexTagBase) and not ignore_auto:
                raise RuntimeError("cannot find grid sizes if automatic "
                        "local index tags are present")
            else:
                tgt_dict = None

            if tgt_dict is None:
                continue

            size = self.get_iname_bounds(iname).size

            if tag.axis in tgt_dict:
                size = tgt_dict[tag.axis].max(size)

            from loopy.isl_helpers import static_max_of_pw_aff
            try:
                # insist block size is constant
                size = static_max_of_pw_aff(size,
                        constants_only=isinstance(tag, LocalIndexTag))
            except ValueError:
                pass

            tgt_dict[tag.axis] = size

        def to_dim_tuple(size_dict, which, forced_sizes={}):
            forced_sizes = forced_sizes.copy()

            size_list = []
            sorted_axes = sorted(six.iterkeys(size_dict))

            while sorted_axes or forced_sizes:
                if sorted_axes:
                    cur_axis = sorted_axes.pop(0)
                else:
                    cur_axis = None

                if len(size_list) in forced_sizes:
                    size_list.append(forced_sizes.pop(len(size_list)))
                    continue

                assert cur_axis is not None

                if cur_axis > len(size_list):
                    raise RuntimeError("%s axis %d unused" % (
                        which, len(size_list)))

                size_list.append(size_dict[cur_axis])

            return tuple(size_list)

        return (to_dim_tuple(global_sizes, "global"),
                to_dim_tuple(local_sizes, "local", forced_sizes=self.local_sizes))

    def get_grid_sizes_as_exprs(self, ignore_auto=False):
        grid_size, group_size = self.get_grid_sizes(ignore_auto)

        def tup_to_exprs(tup):
            from loopy.symbolic import pw_aff_to_expr
            return tuple(pw_aff_to_expr(i, int_ok=True) for i in tup)

        return tup_to_exprs(grid_size), tup_to_exprs(group_size)

    # }}}

    # {{{ local memory

    @memoize_method
    def local_var_names(self):
        return set(
            tv.name
            for tv in six.itervalues(self.temporary_variables)
            if tv.is_local)

    def local_mem_use(self):
        return sum(lv.nbytes for lv in six.itervalues(self.temporary_variables)
                if lv.is_local)

    # }}}

    # {{{ pretty-printing

    def __str__(self):
        lines = []

        from loopy.preprocess import add_default_dependencies
        kernel = add_default_dependencies(self)

        sep = 75*"-"
        lines.append(sep)
        lines.append("KERNEL: " + kernel.name)
        lines.append(sep)
        lines.append("ARGUMENTS:")
        for arg_name in sorted(kernel.arg_dict):
            lines.append(str(kernel.arg_dict[arg_name]))
        lines.append(sep)
        lines.append("DOMAINS:")
        for dom, parents in zip(kernel.domains, kernel.all_parents_per_domain()):
            lines.append(len(parents)*"  " + str(dom))

        lines.append(sep)
        lines.append("INAME IMPLEMENTATION TAGS:")
        for iname in sorted(kernel.all_inames()):
            line = "%s: %s" % (iname, kernel.iname_to_tag.get(iname))
            lines.append(line)

        if kernel.temporary_variables:
            lines.append(sep)
            lines.append("TEMPORARIES:")
            for tv in sorted(six.itervalues(kernel.temporary_variables),
                    key=lambda tv: tv.name):
                lines.append(str(tv))

        if kernel.substitutions:
            lines.append(sep)
            lines.append("SUBSTIUTION RULES:")
            for rule_name in sorted(six.iterkeys(kernel.substitutions)):
                lines.append(str(kernel.substitutions[rule_name]))

        lines.append(sep)
        lines.append("INSTRUCTIONS:")
        loop_list_width = 35

        printed_insn_ids = set()

        def print_insn(insn):
            if insn.id in printed_insn_ids:
                return
            printed_insn_ids.add(insn.id)

            for dep_id in insn.insn_deps:
                print_insn(kernel.id_to_insn[dep_id])

            if isinstance(insn, lp.ExpressionInstruction):
                lhs = str(insn.assignee)
                rhs = str(insn.expression)
                trailing = []
            elif isinstance(insn, lp.CInstruction):
                lhs = ", ".join(str(a) for a in insn.assignees)
                rhs = "CODE(%s|%s)" % (
                        ", ".join(str(x) for x in insn.read_variables),
                        ", ".join("%s=%s" % (name, expr)
                            for name, expr in insn.iname_exprs))

                trailing = ["    "+l for l in insn.code.split("\n")]

            loop_list = ",".join(sorted(kernel.insn_inames(insn)))

            options = [insn.id]
            if insn.priority:
                options.append("priority=%d" % insn.priority)
            if insn.tags:
                options.append("tags=%s" % ":".join(insn.tags))

            if len(loop_list) > loop_list_width:
                lines.append("[%s]" % loop_list)
                lines.append("%s%s <- %s   # %s" % (
                    (loop_list_width+2)*" ", lhs,
                    rhs, ", ".join(options)))
            else:
                lines.append("[%s]%s%s <- %s   # %s" % (
                    loop_list, " "*(loop_list_width-len(loop_list)),
                    lhs, rhs, ",".join(options)))

            lines.extend(trailing)

            if insn.predicates:
                lines.append(10*" " + "if (%s)" % " && ".join(insn.predicates))

        import loopy as lp
        for insn in kernel.instructions:
            print_insn(insn)

        dep_lines = []
        for insn in kernel.instructions:
            if insn.insn_deps:
                dep_lines.append("%s : %s" % (insn.id, ",".join(insn.insn_deps)))
        if dep_lines:
            lines.append(sep)
            lines.append("DEPENDENCIES: "
                    "(use loopy.show_dependency_graph to visualize)")
            lines.extend(dep_lines)

        lines.append(sep)

        if kernel.schedule is not None:
            lines.append("SCHEDULE:")
            from loopy.schedule import dump_schedule
            lines.append(dump_schedule(kernel.schedule))
            lines.append(sep)

        return "\n".join(lines)

    # }}}

    # {{{ implementation arguments

    @property
    @memoize_method
    def impl_arg_to_arg(self):
        from loopy.kernel.array import ArrayBase

        result = {}

        for arg in self.args:
            if not isinstance(arg, ArrayBase):
                result[arg.name] = arg
                continue

            if arg.shape is None or arg.dim_tags is None:
                result[arg.name] = arg
                continue

            subscripts_and_names = arg.subscripts_and_names()
            if subscripts_and_names is None:
                result[arg.name] = arg
                continue

            for index, sub_arg_name in subscripts_and_names:
                result[sub_arg_name] = arg

        return result

    # }}}

    # {{{ direct execution

    @memoize_method
    def get_compiled_kernel(self, ctx):
        from loopy.compiled import CompiledKernel
        return CompiledKernel(ctx, self)

    def __call__(self, queue, **kwargs):
        return self.get_compiled_kernel(queue.context)(
                queue, **kwargs)

    # }}}

    # {{{ pickling

    def __getstate__(self):
        result = dict(
                (key, getattr(self, key))
                for key in self.__class__.fields
                if hasattr(self, key))

        result.pop("cache_manager", None)

        return result

    def __setstate__(self, state):
        new_fields = set()

        for k, v in six.iteritems(state):
            setattr(self, k, v)
            new_fields.add(k)

        self.register_fields(new_fields)

        from loopy.kernel.tools import SetOperationCacheManager
        self.cache_manager = SetOperationCacheManager()

    # }}}

    # {{{ persistent hash key generation / comparison

    hash_fields = [
            "domains",
            "instructions",
            "args",
            "schedule",
            "name",
            "preambles",
            "assumptions",
            "local_sizes",
            "temporary_variables",
            "iname_to_tag",
            "substitutions",
            "iname_slab_increments",
            "loop_priority",
            "silenced_warnings",
            "options",
            "state",
            "target",
            ]

    comparison_fields = hash_fields + [
            # Contains pymbolic expressions, hence a (small) headache to hash.
            # Likely not needed for hash uniqueness => headache avoided.
            "applied_iname_rewrites",

            # These are lists of functions. It's not clear how to
            # hash these correctly, so let's not attempt it. We'll
            # just assume that the rest of the hash is specific enough
            # that we won't have to rely on differences in these to
            # resolve hash conflicts.

            "preamble_generators",
            "function_manglers",
            "symbol_manglers",
            ]

    def update_persistent_hash(self, key_hash, key_builder):
        """Custom hash computation function for use with
        :class:`pytools.persistent_dict.PersistentDict`.

        Only works in conjunction with :class:`loopy.tools.KeyBuilder`.
        """
        for field_name in self.hash_fields:
            key_builder.rec(key_hash, getattr(self, field_name))

    def __eq__(self, other):
        if not isinstance(other, LoopKernel):
            return False

        for field_name in self.comparison_fields:
            if field_name == "domains":
                for set_a, set_b in zip(self.domains, other.domains):
                    if not set_a.plain_is_equal(set_b):
                        return False

            elif field_name == "assumptions":
                if not self.assumptions.plain_is_equal(other.assumptions):
                    return False

            elif getattr(self, field_name) != getattr(other, field_name):
                return False

        return True

    def __ne__(self, other):
        return not self.__eq__(other)

    # }}}

# }}}

# vim: foldmethod=marker
