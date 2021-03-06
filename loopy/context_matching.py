"""Matching functionality for instruction ids and subsitution
rule invocations stacks."""

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

from six.moves import range, intern


NoneType = type(None)

from pytools.lex import RE


def re_from_glob(s):
    import re
    from fnmatch import translate
    return re.compile("^"+translate(s.strip())+"$")

# {{{ parsing

# {{{ lexer data

_and = intern("and")
_or = intern("or")
_not = intern("not")
_openpar = intern("openpar")
_closepar = intern("closepar")

_id = intern("_id")
_tag = intern("_tag")
_writes = intern("_writes")
_reads = intern("_reads")
_iname = intern("_reads")

_whitespace = intern("_whitespace")

# }}}


_LEX_TABLE = [
    (_and, RE(r"and\b")),
    (_or, RE(r"or\b")),
    (_not, RE(r"not\b")),
    (_openpar, RE(r"\(")),
    (_closepar, RE(r"\)")),

    # TERMINALS
    (_id, RE(r"id:([\w?*]+)")),
    (_tag, RE(r"tag:([\w?*]+)")),
    (_writes, RE(r"writes:([\w?*]+)")),
    (_reads, RE(r"reads:([\w?*]+)")),
    (_iname, RE(r"iname:([\w?*]+)")),

    (_whitespace, RE("[ \t]+")),
    ]


_TERMINALS = ([_id, _tag, _writes, _reads, _iname])

# {{{ operator precedence

_PREC_OR = 10
_PREC_AND = 20
_PREC_NOT = 30

# }}}


# {{{ match expression

class MatchExpressionBase(object):
    def __call__(self, kernel, matchable):
        raise NotImplementedError


class AllMatchExpression(MatchExpressionBase):
    def __call__(self, kernel, matchable):
        return True


class AndMatchExpression(MatchExpressionBase):
    def __init__(self, children):
        self.children = children

    def __call__(self, kernel, matchable):
        return all(ch(kernel, matchable) for ch in self.children)

    def __str__(self):
        return "(%s)" % (" and ".join(str(ch) for ch in self.children))


class OrMatchExpression(MatchExpressionBase):
    def __init__(self, children):
        self.children = children

    def __call__(self, kernel, matchable):
        return any(ch(kernel, matchable) for ch in self.children)

    def __str__(self):
        return "(%s)" % (" or ".join(str(ch) for ch in self.children))


class NotMatchExpression(MatchExpressionBase):
    def __init__(self, child):
        self.child = child

    def __call__(self, kernel, matchable):
        return not self.child(kernel, matchable)

    def __str__(self):
        return "(not %s)" % str(self.child)


class GlobMatchExpressionBase(MatchExpressionBase):
    def __init__(self, glob):
        self.glob = glob

        import re
        from fnmatch import translate
        self.re = re.compile("^"+translate(glob.strip())+"$")

    def __str__(self):
        descr = type(self).__name__
        descr = descr[:descr.find("Match")]
        return descr.lower() + ":" + self.glob


class IdMatchExpression(GlobMatchExpressionBase):
    def __call__(self, kernel, matchable):
        return self.re.match(matchable.id)


class TagMatchExpression(GlobMatchExpressionBase):
    def __call__(self, kernel, matchable):
        if matchable.tags:
            return any(self.re.match(tag) for tag in matchable.tags)
        else:
            return False


class WritesMatchExpression(GlobMatchExpressionBase):
    def __call__(self, kernel, matchable):
        return any(self.re.match(name)
                for name in matchable.write_dependency_names())


class ReadsMatchExpression(GlobMatchExpressionBase):
    def __call__(self, kernel, matchable):
        return any(self.re.match(name)
                for name in matchable.read_dependency_names())


class InameMatchExpression(GlobMatchExpressionBase):
    def __call__(self, kernel, matchable):
        return any(self.re.match(name)
                for name in matchable.inames(kernel))

# }}}


# {{{ parser

def parse_match(expr_str):
    """Syntax examples::

    * ``id:yoink and writes:a_temp``
    * ``id:yoink and (not writes:a_temp or tagged:input)``
    """
    if not expr_str:
        return AllMatchExpression()

    def parse_terminal(pstate):
        next_tag = pstate.next_tag()
        if next_tag is _id:
            result = IdMatchExpression(pstate.next_match_obj().group(1))
            pstate.advance()
            return result
        elif next_tag is _tag:
            result = TagMatchExpression(pstate.next_match_obj().group(1))
            pstate.advance()
            return result
        elif next_tag is _writes:
            result = WritesMatchExpression(pstate.next_match_obj().group(1))
            pstate.advance()
            return result
        elif next_tag is _reads:
            result = ReadsMatchExpression(pstate.next_match_obj().group(1))
            pstate.advance()
            return result
        elif next_tag is _iname:
            result = InameMatchExpression(pstate.next_match_obj().group(1))
            pstate.advance()
            return result
        else:
            pstate.expected("terminal")

    def inner_parse(pstate, min_precedence=0):
        pstate.expect_not_end()

        if pstate.is_next(_not):
            pstate.advance()
            left_query = NotMatchExpression(inner_parse(pstate, _PREC_NOT))
        elif pstate.is_next(_openpar):
            pstate.advance()
            left_query = inner_parse(pstate)
            pstate.expect(_closepar)
            pstate.advance()
        else:
            left_query = parse_terminal(pstate)

        did_something = True
        while did_something:
            did_something = False
            if pstate.is_at_end():
                return left_query

            next_tag = pstate.next_tag()

            if next_tag is _and and _PREC_AND > min_precedence:
                pstate.advance()
                left_query = AndMatchExpression(
                        (left_query, inner_parse(pstate, _PREC_AND)))
                did_something = True
            elif next_tag is _or and _PREC_OR > min_precedence:
                pstate.advance()
                left_query = OrMatchExpression(
                        (left_query, inner_parse(pstate, _PREC_OR)))
                did_something = True

        return left_query

    from pytools.lex import LexIterator, lex, InvalidTokenError
    try:
        pstate = LexIterator(
            [(tag, s, idx, matchobj)
             for (tag, s, idx, matchobj) in lex(_LEX_TABLE, expr_str,
                 match_objects=True)
             if tag is not _whitespace], expr_str)
    except InvalidTokenError as e:
        from loopy.diagnostic import LoopyError
        raise LoopyError(
                "invalid match expression: '{match_expr}' ({err_type}: {err_str})"
                .format(
                    match_expr=expr_str,
                    err_type=type(e).__name__,
                    err_str=str(e)))

    if pstate.is_at_end():
        pstate.raise_parse_error("unexpected end of input")

    result = inner_parse(pstate)
    if not pstate.is_at_end():
        pstate.raise_parse_error("leftover input after completed parse")

    return result

# }}}

# }}}


# {{{ stack match objects

class StackMatchComponent(object):
    pass


class StackAllMatchComponent(StackMatchComponent):
    def __call__(self, kernel, stack):
        return True


class StackBottomMatchComponent(StackMatchComponent):
    def __call__(self, kernel, stack):
        return not stack


class StackItemMatchComponent(StackMatchComponent):
    def __init__(self, match_expr, inner_match):
        self.match_expr = match_expr
        self.inner_match = inner_match

    def __call__(self, kernel, stack):
        if not stack:
            return False

        outer = stack[0]
        if not self.match_expr(kernel, outer):
            return False

        return self.inner_match(kernel, stack[1:])


class StackWildcardMatchComponent(StackMatchComponent):
    def __init__(self, inner_match):
        self.inner_match = inner_match

    def __call__(self, kernel, stack):
        for i in range(0, len(stack)):
            if self.inner_match(kernel, stack[i:]):
                return True

        return False

# }}}


# {{{ stack matcher

class RuleInvocationMatchable(object):
    def __init__(self, id, tags):
        self.id = id
        self.tags = tags

    def write_dependency_names(self):
        raise TypeError("writes: query may not be applied to rule invocations")

    def read_dependency_names(self):
        raise TypeError("reads: query may not be applied to rule invocations")

    def inames(self, kernel):
        raise TypeError("inames: query may not be applied to rule invocations")


class StackMatch(object):
    def __init__(self, root_component):
        self.root_component = root_component

    def __call__(self, kernel, insn, rule_stack):
        """
        :arg rule_stack: a tuple of (name, tags) rule invocation, outermost first
        """
        stack_of_matchables = [insn]
        for id, tags in rule_stack:
            stack_of_matchables.append(RuleInvocationMatchable(id, tags))

        return self.root_component(kernel, stack_of_matchables)

# }}}


# {{{ stack match parsing

def parse_stack_match(smatch):
    """Syntax example::

        ... > outer > ... > next > innermost $
        insn > next
        insn > ... > next > innermost $

    ``...`` matches an arbitrary number of intervening stack levels.

    Each of the entries is a match expression as understood by
    :func:`parse_match`.
    """

    if isinstance(smatch, StackMatch):
        return smatch

    if smatch is None:
        return StackMatch(StackAllMatchComponent())

    smatch = smatch.strip()

    match = StackAllMatchComponent()
    if smatch[-1] == "$":
        match = StackBottomMatchComponent()
        smatch = smatch[:-1]

    smatch = smatch.strip()

    components = smatch.split(">")

    for comp in components[::-1]:
        comp = comp.strip()
        if comp == "...":
            match = StackWildcardMatchComponent(match)
        else:
            match = StackItemMatchComponent(parse_match(comp), match)

    return StackMatch(match)

# }}}


# vim: foldmethod=marker
