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

from loopy.diagnostic import LoopyError, warn
from pytools import Record
import islpy as isl

import numpy as np

from pytools.persistent_dict import PersistentDict
from loopy.tools import LoopyKeyBuilder
from loopy.version import DATA_MODEL_VERSION

import logging
logger = logging.getLogger(__name__)


# {{{ support code for AST wrapper objects

class GeneratedInstruction(Record):
    """Objects of this type are wrapped around ASTs upon
    return from generation calls to collect information about them.

    :ivar implemented_domains: A map from an insn id to a list of
        implemented domains, with the purpose of checking that
        each instruction's exact iteration space has been covered.
    """
    __slots__ = ["insn_id", "implemented_domain", "ast"]


class GeneratedCode(Record):
    """Objects of this type are wrapped around ASTs upon
    return from generation calls to collect information about them.

    :ivar implemented_domains: A map from an insn id to a list of
        implemented domains, with the purpose of checking that
        each instruction's exact iteration space has been covered.
    """
    __slots__ = ["ast", "implemented_domains"]


def gen_code_block(elements):
    from cgen import Block, Comment, Line, Initializer

    block_els = []
    implemented_domains = {}

    for el in elements:
        if isinstance(el, GeneratedCode):
            for insn_id, idoms in six.iteritems(el.implemented_domains):
                implemented_domains.setdefault(insn_id, []).extend(idoms)

            if isinstance(el.ast, Block):
                block_els.extend(el.ast.contents)
            else:
                block_els.append(el.ast)

        elif isinstance(el, Initializer):
            block_els.append(el)

        elif isinstance(el, Comment):
            block_els.append(el)

        elif isinstance(el, Line):
            assert not el.text
            block_els.append(el)

        elif isinstance(el, GeneratedInstruction):
            block_els.append(el.ast)
            if el.implemented_domain is not None:
                implemented_domains.setdefault(el.insn_id, []).append(
                        el.implemented_domain)

        else:
            raise ValueError("unrecognized object of type '%s' in block"
                    % type(el))

    if len(block_els) == 1:
        ast, = block_els
    else:
        ast = Block(block_els)

    return GeneratedCode(ast=ast, implemented_domains=implemented_domains)


def wrap_in(cls, *args):
    inner = args[-1]
    args = args[:-1]

    if not isinstance(inner, GeneratedCode):
        raise ValueError("unrecognized object of type '%s' in block"
                % type(inner))

    args = args + (inner.ast,)

    return GeneratedCode(ast=cls(*args),
            implemented_domains=inner.implemented_domains)


def wrap_in_if(condition_codelets, inner):
    from cgen import If

    if condition_codelets:
        return wrap_in(If,
                "\n&& ".join(condition_codelets),
                inner)

    return inner


def add_comment(cmt, code):
    if cmt is None:
        return code

    from cgen import add_comment
    assert isinstance(code, GeneratedCode)

    return GeneratedCode(
            ast=add_comment(cmt, code.ast),
            implemented_domains=code.implemented_domains)

# }}}


class SeenFunction(Record):
    """
    .. attribute:: name
    .. attribute:: c_name
    .. attribute:: arg_dtypes

        a tuple of arg dtypes
    """

    def __init__(self, name, c_name, arg_dtypes):
        Record.__init__(self,
                name=name,
                c_name=c_name,
                arg_dtypes=arg_dtypes)

    def __hash__(self):
        return hash((type(self),)
                + tuple((f, getattr(self, f)) for f in type(self).fields))


# {{{ code generation state

class Unvectorizable(Exception):
    pass


class VectorizationInfo(object):
    """
    .. attribute:: iname
    .. attribute:: length
    .. attribute:: space
    """

    def __init__(self, iname, length, space):
        self.iname = iname
        self.length = length
        self.space = space


class CodeGenerationState(object):
    """
    .. attribute:: kernel
    .. attribute:: implemented_domain

        The entire implemented domain (as an :class:`islpy.Set`)
        i.e. all constraints that have been enforced so far.

    .. attribute:: implemented_predicates

        A :class:`frozenset` of predicates for which checks have been
        implemented.

    .. attribute:: seen_dtypes

        set of dtypes that were encountered

    .. attribute:: seen_functions

        set of :class:`SeenFunction` instances

    .. attribute:: var_subst_map

    .. attribute:: allow_complex

    .. attribute:: vectorization_info

        None or an instance of :class:`VectorizationInfo`
    """

    def __init__(self, kernel, implemented_domain, implemented_predicates,
            seen_dtypes, seen_functions, var_subst_map,
            allow_complex,
            vectorization_info=None):
        self.kernel = kernel
        self.implemented_domain = implemented_domain
        self.implemented_predicates = implemented_predicates
        self.seen_dtypes = seen_dtypes
        self.seen_functions = seen_functions
        self.var_subst_map = var_subst_map.copy()
        self.allow_complex = allow_complex
        self.vectorization_info = vectorization_info

    # {{{ copy helpers

    def copy(self, implemented_domain=None, implemented_predicates=frozenset(),
            var_subst_map=None, vectorization_info=None):

        if vectorization_info is False:
            vectorization_info = None

        elif vectorization_info is None:
            vectorization_info = self.vectorization_info

        return CodeGenerationState(
                kernel=self.kernel,
                implemented_domain=implemented_domain or self.implemented_domain,
                implemented_predicates=(
                    implemented_predicates or self.implemented_predicates),
                seen_dtypes=self.seen_dtypes,
                seen_functions=self.seen_functions,
                var_subst_map=var_subst_map or self.var_subst_map,
                allow_complex=self.allow_complex,
                vectorization_info=vectorization_info)

    def copy_and_assign(self, name, value):
        """Make a copy of self with variable *name* fixed to *value*."""
        var_subst_map = self.var_subst_map.copy()
        var_subst_map[name] = value
        return self.copy(var_subst_map=var_subst_map)

    def copy_and_assign_many(self, assignments):
        """Make a copy of self with *assignments* included."""

        var_subst_map = self.var_subst_map.copy()
        var_subst_map.update(assignments)
        return self.copy(var_subst_map=var_subst_map)

    # }}}

    @property
    def expression_to_code_mapper(self):
        # It's kind of unfortunate that this is here, but it's an accident
        # of history for now.

        return self.kernel.target.get_expression_to_code_mapper(self)

    def intersect(self, other):
        new_impl, new_other = isl.align_two(self.implemented_domain, other)
        return self.copy(implemented_domain=new_impl & new_other)

    def fix(self, iname, aff):
        new_impl_domain = self.implemented_domain

        impl_space = self.implemented_domain.get_space()
        if iname not in impl_space.get_var_dict():
            new_impl_domain = (new_impl_domain
                    .add_dims(isl.dim_type.set, 1)
                    .set_dim_name(
                        isl.dim_type.set,
                        new_impl_domain.dim(isl.dim_type.set),
                        iname))
            impl_space = new_impl_domain.get_space()

        from loopy.isl_helpers import iname_rel_aff
        iname_plus_lb_aff = iname_rel_aff(impl_space, iname, "==", aff)

        from loopy.symbolic import pw_aff_to_expr
        cns = isl.Constraint.equality_from_aff(iname_plus_lb_aff)
        expr = pw_aff_to_expr(aff)

        new_impl_domain = new_impl_domain.add_constraint(cns)
        return self.copy_and_assign(iname, expr).copy(
                implemented_domain=new_impl_domain)

    def try_vectorized(self, what, func):
        """If *self* is in a vectorizing state (:attr:`vectorization_info` is
        not None), tries to call func (which must be a callable accepting a
        single :class:`CodeGenerationState` argument). If this fails with
        :exc:`Unvectorizable`, it unrolls the vectorized loop instead.

        *func* should return a :class:`GeneratedCode` instance.

        :returns: :class:`GeneratedCode`
        """

        if self.vectorization_info is None:
            return func(self)

        try:
            return func(self)
        except Unvectorizable as e:
            warn(self.kernel, "vectorize_failed",
                    "Vectorization of '%s' failed because '%s'"
                    % (what, e))

            return self.unvectorize(func)

    def unvectorize(self, func):
        vinf = self.vectorization_info
        result = []
        novec_self = self.copy(vectorization_info=False)

        for i in range(vinf.length):
            idx_aff = isl.Aff.zero_on_domain(vinf.space.params()) + i
            new_codegen_state = novec_self.fix(vinf.iname, idx_aff)
            result.append(func(new_codegen_state))

        return gen_code_block(result)
# }}}


# {{{ cgen overrides

from cgen import Declarator


class POD(Declarator):
    """A simple declarator: The type is given as a :class:`numpy.dtype`
    and the *name* is given as a string.
    """

    def __init__(self, target, dtype, name):
        dtype = np.dtype(dtype)

        self.target = target
        self.ctype = target.dtype_to_typename(dtype)
        self.dtype = dtype
        self.name = name

    def get_decl_pair(self):
        return [self.ctype], self.name

    def struct_maker_code(self, name):
        return name

    def struct_format(self):
        return self.dtype.char

    def alignment_requirement(self):
        return self.target.alignment_requirement(self)

    def default_value(self):
        return 0

# }}}


# {{{ implemented data info

class ImplementedDataInfo(Record):
    """
    .. attribute:: name

        The expanded name of the array. Note that, for example
        in the case of separate-array-tagged axes, multiple
        implemented arrays may correspond to one user-facing
        array.

    .. attribute:: dtype
    .. attribute:: cgen_declarator

        Declarator syntax tree as a :mod:`cgen` object.

    .. attribute:: arg_class

    .. attribute:: base_name

        The user-facing name of the underlying array.
        May be *None* for non-array arguments.

    .. attribute:: shape
    .. attribute:: strides

        Strides in multiples of ``dtype.itemsize``.

    .. attribute:: unvec_shape
    .. attribute:: unvec_strides

        Strides in multiples of ``dtype.itemsize`` that accounts for
        :class:`loopy.kernel.array.VectorArrayDimTag` in a scalar
        manner


    .. attribute:: offset_for_name
    .. attribute:: stride_for_name_and_axis

        A tuple *(name, axis)* indicating the (implementation-facing)
        name of the array and axis number for which this argument provides
        the strides.

    .. attribute:: allows_offset
    """

    def __init__(self, target, name, dtype, cgen_declarator, arg_class,
            base_name=None,
            shape=None, strides=None,
            unvec_shape=None, unvec_strides=None,
            offset_for_name=None, stride_for_name_and_axis=None,
            allows_offset=None):

        from loopy.tools import PicklableDtype

        Record.__init__(self,
                name=name,
                picklable_dtype=PicklableDtype(dtype, target=target),
                cgen_declarator=cgen_declarator,
                arg_class=arg_class,
                base_name=base_name,
                shape=shape,
                strides=strides,
                unvec_shape=unvec_shape,
                unvec_strides=unvec_strides,
                offset_for_name=offset_for_name,
                stride_for_name_and_axis=stride_for_name_and_axis,
                allows_offset=allows_offset)

    @property
    def dtype(self):
        from loopy.tools import PicklableDtype
        if isinstance(self.picklable_dtype, PicklableDtype):
            return self.picklable_dtype.dtype
        else:
            return self.picklable_dtype

# }}}


code_gen_cache = PersistentDict("loopy-code-gen-cache-v3-"+DATA_MODEL_VERSION,
        key_builder=LoopyKeyBuilder())


# {{{ main code generation entrypoint

def generate_code(kernel, device=None):
    if device is not None:
        from warnings import warn
        warn("passing 'device' to generate_code() is deprecated",
                DeprecationWarning, stacklevel=2)

    if kernel.schedule is None:
        from loopy.schedule import get_one_scheduled_kernel
        kernel = get_one_scheduled_kernel(kernel)
    from loopy.kernel import kernel_state
    if kernel.state != kernel_state.SCHEDULED:
        raise LoopyError("cannot generate code for a kernel that has not been "
                "scheduled")

    # {{{ cache retrieval

    from loopy import CACHING_ENABLED

    if CACHING_ENABLED:
        input_kernel = kernel
        try:
            result = code_gen_cache[input_kernel]
            logger.info("%s: code generation cache hit" % kernel.name)
            return result
        except KeyError:
            pass

    # }}}

    from loopy.preprocess import infer_unknown_types
    kernel = infer_unknown_types(kernel, expect_completion=True)

    from loopy.check import pre_codegen_checks
    pre_codegen_checks(kernel)

    logger.info("%s: generate code: start" % kernel.name)

    # {{{ examine arg list

    from loopy.kernel.data import ValueArg
    from loopy.kernel.array import ArrayBase
    from cgen import Const

    impl_arg_info = []

    for arg in kernel.args:
        if isinstance(arg, ArrayBase):
            impl_arg_info.extend(
                    arg.decl_info(
                        kernel.target,
                        is_written=arg.name in kernel.get_written_variables(),
                        index_dtype=kernel.index_dtype))

        elif isinstance(arg, ValueArg):
            impl_arg_info.append(ImplementedDataInfo(
                target=kernel.target,
                name=arg.name,
                dtype=arg.dtype,
                cgen_declarator=Const(POD(kernel.target, arg.dtype, arg.name)),
                arg_class=ValueArg))

        else:
            raise ValueError("argument type not understood: '%s'" % type(arg))

    allow_complex = False
    for var in kernel.args + list(six.itervalues(kernel.temporary_variables)):
        if var.dtype.kind == "c":
            allow_complex = True

    # }}}

    seen_dtypes = set()
    seen_functions = set()

    initial_implemented_domain = isl.BasicSet.from_params(kernel.assumptions)
    codegen_state = CodeGenerationState(
            kernel=kernel,
            implemented_domain=initial_implemented_domain,
            implemented_predicates=frozenset(),
            seen_dtypes=seen_dtypes,
            seen_functions=seen_functions,
            var_subst_map={},
            allow_complex=allow_complex)

    code_str, implemented_domains = kernel.target.generate_code(
            kernel, codegen_state, impl_arg_info)

    from loopy.check import check_implemented_domains
    assert check_implemented_domains(kernel, implemented_domains,
            code_str)

    # {{{ handle preambles

    for arg in kernel.args:
        seen_dtypes.add(arg.dtype)
    for tv in six.itervalues(kernel.temporary_variables):
        seen_dtypes.add(tv.dtype)

    preambles = kernel.preambles[:]

    preamble_generators = (kernel.preamble_generators
            + kernel.target.preamble_generators())
    for prea_gen in preamble_generators:
        preambles.extend(prea_gen(kernel.target, seen_dtypes, seen_functions))

    seen_preamble_tags = set()
    dedup_preambles = []

    for tag, preamble in sorted(preambles, key=lambda tag_code: tag_code[0]):
        if tag in seen_preamble_tags:
            continue

        seen_preamble_tags.add(tag)
        dedup_preambles.append(preamble)

    from loopy.tools import remove_common_indentation
    preamble_codes = [
            remove_common_indentation(lines) + "\n"
            for lines in dedup_preambles]

    code_str = "".join(preamble_codes) + code_str

    # }}}

    logger.info("%s: generate code: done" % kernel.name)

    result = code_str, impl_arg_info

    if CACHING_ENABLED:
        code_gen_cache[input_kernel] = result

    return result

# }}}


# {{{ generate function body

def generate_body(kernel):
    if kernel.schedule is None:
        from loopy.schedule import get_one_scheduled_kernel
        kernel = get_one_scheduled_kernel(kernel)
    from loopy.kernel import kernel_state
    if kernel.state != kernel_state.SCHEDULED:
        raise LoopyError("cannot generate code for a kernel that has not been "
                "scheduled")

    from loopy.preprocess import infer_unknown_types
    kernel = infer_unknown_types(kernel, expect_completion=True)

    from loopy.check import pre_codegen_checks
    pre_codegen_checks(kernel)

    logger.info("%s: generate code: start" % kernel.name)

    allow_complex = False
    for var in kernel.args + list(six.itervalues(kernel.temporary_variables)):
        if var.dtype.kind == "c":
            allow_complex = True

    seen_dtypes = set()
    seen_functions = set()

    initial_implemented_domain = isl.BasicSet.from_params(kernel.assumptions)
    codegen_state = CodeGenerationState(
            kernel=kernel,
            implemented_domain=initial_implemented_domain,
            implemented_predicates=frozenset(),
            seen_dtypes=seen_dtypes,
            seen_functions=seen_functions,
            var_subst_map={},
            allow_complex=allow_complex)

    code_str, implemented_domains = kernel.target.generate_body(
            kernel, codegen_state)

    from loopy.check import check_implemented_domains
    assert check_implemented_domains(kernel, implemented_domains,
            code_str)

    logger.info("%s: generate code: done" % kernel.name)

    return code_str

# }}}

# vim: foldmethod=marker
