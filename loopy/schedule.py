from __future__ import division, absolute_import, print_function

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
from pytools import Record
import sys
import islpy as isl
from loopy.diagnostic import LoopyError  # noqa

from pytools.persistent_dict import PersistentDict
from loopy.tools import LoopyKeyBuilder
from loopy.version import DATA_MODEL_VERSION

import logging
logger = logging.getLogger(__name__)


# {{{ schedule items

class ScheduleItem(Record):
    __slots__ = []

    def update_persistent_hash(self, key_hash, key_builder):
        """Custom hash computation function for use with
        :class:`pytools.persistent_dict.PersistentDict`.
        """
        for field_name in self.hash_fields:
            key_builder.rec(key_hash, getattr(self, field_name))


class EnterLoop(ScheduleItem):
    hash_fields = __slots__ = ["iname"]


class LeaveLoop(ScheduleItem):
    hash_fields = __slots__ = ["iname"]


class RunInstruction(ScheduleItem):
    hash_fields = __slots__ = ["insn_id"]


class Barrier(ScheduleItem):
    """
    .. attribute:: comment

        A plain-text comment explaining why the barrier was inserted.

    .. attribute:: kind

        ``"local"`` or ``"global"``
    """
    hash_fields = __slots__ = ["comment", "kind"]

# }}}


# {{{ schedule utilities

def gather_schedule_subloop(schedule, start_idx):
    assert isinstance(schedule[start_idx], EnterLoop)
    level = 0

    i = start_idx
    while i < len(schedule):
        if isinstance(schedule[i], EnterLoop):
            level += 1
        if isinstance(schedule[i], LeaveLoop):
            level -= 1

            if level == 0:
                return schedule[start_idx:i+1], i+1

        i += 1

    assert False


def generate_sub_sched_items(schedule, start_idx):
    if not isinstance(schedule[start_idx], EnterLoop):
        yield start_idx, schedule[start_idx]

    level = 0
    i = start_idx
    while i < len(schedule):
        sched_item = schedule[i]
        if isinstance(sched_item, EnterLoop):
            level += 1
        elif isinstance(sched_item, LeaveLoop):
            level -= 1

        else:
            yield i, sched_item

        if level == 0:
            return

        i += 1

    assert False


def find_active_inames_at(kernel, sched_index):
    active_inames = []

    from loopy.schedule import EnterLoop, LeaveLoop
    for sched_item in kernel.schedule[:sched_index]:
        if isinstance(sched_item, EnterLoop):
            active_inames.append(sched_item.iname)
        if isinstance(sched_item, LeaveLoop):
            active_inames.pop()

    return set(active_inames)


def has_barrier_within(kernel, sched_index):
    sched_item = kernel.schedule[sched_index]

    if isinstance(sched_item, EnterLoop):
        loop_contents, _ = gather_schedule_subloop(
                kernel.schedule, sched_index)
        from pytools import any
        return any(isinstance(subsched_item, Barrier)
                for subsched_item in loop_contents)
    elif isinstance(sched_item, Barrier):
        return True
    else:
        return False


def find_used_inames_within(kernel, sched_index):
    sched_item = kernel.schedule[sched_index]

    if isinstance(sched_item, EnterLoop):
        loop_contents, _ = gather_schedule_subloop(
                kernel.schedule, sched_index)
        run_insns = [subsched_item
                for subsched_item in loop_contents
                if isinstance(subsched_item, RunInstruction)]
    elif isinstance(sched_item, RunInstruction):
        run_insns = [sched_item]
    else:
        return set()

    result = set()
    for sched_item in run_insns:
        result.update(kernel.insn_inames(sched_item.insn_id))

    return result


def loop_nest_map(kernel):
    """Returns a dictionary mapping inames to other inames that are
    always nested around them.
    """
    result = {}

    all_inames = kernel.all_inames()

    iname_to_insns = kernel.iname_to_insns()

    # examine pairs of all inames--O(n**2), I know.
    from loopy.kernel.data import IlpBaseTag
    for inner_iname in all_inames:
        result[inner_iname] = set()
        for outer_iname in all_inames:
            if inner_iname == outer_iname:
                continue

            tag = kernel.iname_to_tag.get(outer_iname)
            if isinstance(tag, IlpBaseTag):
                # ILP tags are special because they are parallel tags
                # and therefore 'in principle' nest around everything.
                # But they're realized by the scheduler as a loop
                # at the innermost level, so we'll cut them some
                # slack here.
                continue

            if iname_to_insns[inner_iname] < iname_to_insns[outer_iname]:
                result[inner_iname].add(outer_iname)

    for dom_idx, dom in enumerate(kernel.domains):
        for outer_iname in dom.get_var_names(isl.dim_type.param):
            if outer_iname not in all_inames:
                continue

            for inner_iname in dom.get_var_names(isl.dim_type.set):
                result[inner_iname].add(outer_iname)

    return result

# }}}


# {{{ debug help

def dump_schedule(schedule):
    entries = []
    for sched_item in schedule:
        if isinstance(sched_item, EnterLoop):
            entries.append("<%s>" % sched_item.iname)
        elif isinstance(sched_item, LeaveLoop):
            entries.append("</%s>" % sched_item.iname)
        elif isinstance(sched_item, RunInstruction):
            entries.append(sched_item.insn_id)
        elif isinstance(sched_item, Barrier):
            entries.append("|")
        else:
            assert False

    return " ".join(entries)


class ScheduleDebugger:
    def __init__(self, debug_length=None, interactive=True):
        self.longest_rejected_schedule = []
        self.success_counter = 0
        self.dead_end_counter = 0
        self.debug_length = debug_length
        self.interactive = interactive

        self.elapsed_store = 0
        self.start()
        self.wrote_status = 0

        self.update()

    def update(self):
        if (
                (self.success_counter + self.dead_end_counter) % 50 == 0
                and self.elapsed_time() > 10
                ):
            sys.stdout.write("\rscheduling... %d successes, "
                    "%d dead ends (longest %d)" % (
                        self.success_counter,
                        self.dead_end_counter,
                        len(self.longest_rejected_schedule)))
            sys.stdout.flush()
            self.wrote_status = 2

    def log_success(self, schedule):
        self.success_counter += 1
        self.update()

    def log_dead_end(self, schedule):
        if len(schedule) > len(self.longest_rejected_schedule):
            self.longest_rejected_schedule = schedule
        self.dead_end_counter += 1
        self.update()

    def done_scheduling(self):
        if self.wrote_status:
            sys.stdout.write("\rscheduler finished"+40*" "+"\n")
            sys.stdout.flush()

    def elapsed_time(self):
        from time import time
        return self.elapsed_store + time() - self.start_time

    def stop(self):
        if self.wrote_status == 2:
            sys.stdout.write("\r"+80*" "+"\n")
            self.wrote_status = 1

        from time import time
        self.elapsed_store += time()-self.start_time

    def start(self):
        from time import time
        self.start_time = time()
# }}}


# {{{ scheduling algorithm

class SchedulerState(Record):
    pass


def generate_loop_schedules_internal(sched_state, loop_priority, schedule=[],
        allow_boost=False, allow_insn=False, debug=None):
    # allow_insn is set to False initially and after entering each loop
    # to give loops containing high-priority instructions a chance.

    kernel = sched_state.kernel
    all_insn_ids = set(insn.id for insn in kernel.instructions)

    scheduled_insn_ids = set(sched_item.insn_id for sched_item in schedule
            if isinstance(sched_item, RunInstruction))

    unscheduled_insn_ids = all_insn_ids - scheduled_insn_ids

    if allow_boost is None:
        rec_allow_boost = None
    else:
        rec_allow_boost = False

    # {{{ find active and entered loops

    active_inames = []
    entered_inames = set()

    for sched_item in schedule:
        if isinstance(sched_item, EnterLoop):
            active_inames.append(sched_item.iname)
            entered_inames.add(sched_item.iname)
        if isinstance(sched_item, LeaveLoop):
            active_inames.pop()

    if active_inames:
        last_entered_loop = active_inames[-1]
    else:
        last_entered_loop = None
    active_inames_set = set(active_inames)

    # }}}

    # {{{ decide about debug mode

    debug_mode = False

    if debug is not None:
        if (debug.debug_length is not None
                and len(schedule) >= debug.debug_length):
            debug_mode = True

    if debug_mode:
        if debug.wrote_status == 2:
            print()
        print(75*"=")
        print("KERNEL:")
        print(kernel)
        print(75*"=")
        print("CURRENT SCHEDULE:")
        print("%s (length: %d)" % (dump_schedule(schedule), len(schedule)))
        print("(LEGEND: entry into loop: <iname>, exit from loop: </iname>, "
                "instructions w/ no delimiters)")
        #print("boost allowed:", allow_boost)
        print(75*"=")
        print("LOOP NEST MAP:")
        for iname, val in six.iteritems(sched_state.loop_nest_map):
            print("%s : %s" % (iname, ", ".join(val)))
        print(75*"=")
        print("WHY IS THIS A DEAD-END SCHEDULE?")

    #if len(schedule) == 2:
        #from pudb import set_trace; set_trace()

    # }}}

    # {{{ see if any insns are ready to be scheduled now

    # Also take note of insns that have a chance of being schedulable inside
    # the current loop nest, in this set:

    reachable_insn_ids = set()

    for insn_id in sorted(unscheduled_insn_ids,
            key=lambda insn_id: kernel.id_to_insn[insn_id].priority,
            reverse=True):

        insn = kernel.id_to_insn[insn_id]

        is_ready = set(insn.insn_deps) <= scheduled_insn_ids

        if not is_ready:
            if debug_mode:
                print("instruction '%s' is missing insn depedencies '%s'" % (
                        insn.id, ",".join(set(insn.insn_deps) - scheduled_insn_ids)))
            continue

        want = kernel.insn_inames(insn) - sched_state.parallel_inames
        have = active_inames_set - sched_state.parallel_inames

        # If insn is boostable, it may be placed inside a more deeply
        # nested loop without harm.

        if allow_boost:
            # Note that the inames in 'insn.boostable_into' necessarily won't
            # be contained in 'want'.
            have = have - insn.boostable_into

        if want != have:
            is_ready = False

            if debug_mode:
                if want-have:
                    print("instruction '%s' is missing inames '%s'"
                            % (insn.id, ",".join(want-have)))
                if have-want:
                    print("instruction '%s' won't work under inames '%s'"
                            % (insn.id, ",".join(have-want)))

        # {{{ determine reachability

        if (not is_ready and have <= want):
            reachable_insn_ids.add(insn_id)

        # }}}

        if is_ready and allow_insn:
            if debug_mode:
                print("scheduling '%s'" % insn.id)
            scheduled_insn_ids.add(insn.id)
            schedule = schedule + [RunInstruction(insn_id=insn.id)]

            # Don't be eager about entering/leaving loops--if progress has been
            # made, revert to top of scheduler and see if more progress can be
            # made.

            for sub_sched in generate_loop_schedules_internal(
                    sched_state, loop_priority, schedule,
                    allow_boost=rec_allow_boost, debug=debug,
                    allow_insn=True):
                yield sub_sched

            return

    # }}}

    # {{{ see if we're ready to leave the innermost loop

    if last_entered_loop is not None:
        can_leave = True

        if last_entered_loop not in sched_state.breakable_inames:
            # If the iname is not breakable, then check that we've
            # scheduled all the instructions that require it.

            for insn_id in unscheduled_insn_ids:
                insn = kernel.id_to_insn[insn_id]
                if last_entered_loop in kernel.insn_inames(insn):
                    if debug_mode:
                        print("cannot leave '%s' because '%s' still depends on it"
                                % (last_entered_loop, insn.id))
                    can_leave = False
                    break

        if can_leave:
            can_leave = False

            # We may only leave this loop if we've scheduled an instruction
            # since entering it.

            seen_an_insn = False
            ignore_count = 0
            for sched_item in schedule[::-1]:
                if isinstance(sched_item, RunInstruction):
                    seen_an_insn = True
                elif isinstance(sched_item, LeaveLoop):
                    ignore_count += 1
                elif isinstance(sched_item, EnterLoop):
                    if ignore_count:
                        ignore_count -= 1
                    else:
                        assert sched_item.iname == last_entered_loop
                        if seen_an_insn:
                            can_leave = True
                        break

            if can_leave:
                schedule = schedule + [LeaveLoop(iname=last_entered_loop)]

                for sub_sched in generate_loop_schedules_internal(
                        sched_state, loop_priority, schedule,
                        allow_boost=rec_allow_boost, debug=debug,
                        allow_insn=allow_insn):
                    yield sub_sched

                return

    # }}}

    # {{{ see if any loop can be entered now

    # Find inames that are being referenced by as yet unscheduled instructions.
    needed_inames = set()
    for insn_id in unscheduled_insn_ids:
        needed_inames.update(kernel.insn_inames(insn_id))

    needed_inames = (needed_inames
            # There's no notion of 'entering' a parallel loop
            - sched_state.parallel_inames

            # Don't reenter a loop we're already in.
            - active_inames_set)

    if debug_mode:
        print(75*"-")
        print("inames still needed :", ",".join(needed_inames))
        print("active inames :", ",".join(active_inames))
        print("inames entered so far :", ",".join(entered_inames))
        print("reachable insns:", ",".join(reachable_insn_ids))
        print(75*"-")

    if needed_inames:
        iname_to_usefulness = {}

        for iname in needed_inames:

            # {{{ check if scheduling this iname now is allowed/plausible

            currently_accessible_inames = (
                    active_inames_set | sched_state.parallel_inames)
            if not sched_state.loop_nest_map[iname] <= currently_accessible_inames:
                if debug_mode:
                    print("scheduling %s prohibited by loop nest map" % iname)
                continue

            iname_home_domain = kernel.domains[kernel.get_home_domain_index(iname)]
            from islpy import dim_type
            iname_home_domain_params = set(
                    iname_home_domain.get_var_names(dim_type.param))

            # The previous check should have ensured this is true, because
            # the loop_nest_map takes the domain dependency graph into
            # consideration.
            assert (iname_home_domain_params & kernel.all_inames()
                    <= currently_accessible_inames)

            # Check if any parameters are temporary variables, and if so, if their
            # writes have already been scheduled.

            data_dep_written = True
            for domain_par in (
                    iname_home_domain_params
                    &
                    set(kernel.temporary_variables)):
                writer_insn, = kernel.writer_map()[domain_par]
                if writer_insn not in scheduled_insn_ids:
                    data_dep_written = False
                    break

            if not data_dep_written:
                continue

            # }}}

            # {{{ determine if that gets us closer to being able to schedule an insn

            usefulness = None  # highest insn priority enabled by iname

            hypothetically_active_loops = active_inames_set | set([iname])
            for insn_id in reachable_insn_ids:
                insn = kernel.id_to_insn[insn_id]

                want = kernel.insn_inames(insn) | insn.boostable_into

                if hypothetically_active_loops <= want:
                    if usefulness is None:
                        usefulness = insn.priority
                    else:
                        usefulness = max(usefulness, insn.priority)

            if usefulness is None:
                if debug_mode:
                    print("iname '%s' deemed not useful" % iname)
                continue

            iname_to_usefulness[iname] = usefulness

            # }}}

        # {{{ tier building

        # Build priority tiers. If a schedule is found in the first tier, then
        # loops in the second are not even tried (and so on).

        loop_priority_set = set(loop_priority)
        useful_loops_set = set(six.iterkeys(iname_to_usefulness))
        useful_and_desired = useful_loops_set & loop_priority_set

        if useful_and_desired:
            priority_tiers = [
                    [iname]
                    for iname in loop_priority
                    if iname in useful_and_desired
                    and iname not in sched_state.ilp_inames
                    and iname not in sched_state.vec_inames
                    ]

            priority_tiers.append(
                    useful_loops_set
                    - loop_priority_set
                    - sched_state.ilp_inames
                    - sched_state.vec_inames
                    )
        else:
            priority_tiers = [
                    useful_loops_set
                    - sched_state.ilp_inames
                    - sched_state.vec_inames
                    ]

        # vectorization must be the absolute innermost loop
        priority_tiers.extend([
            [iname]
            for iname in sched_state.ilp_inames
            if iname in useful_loops_set
            ])

        priority_tiers.extend([
            [iname]
            for iname in sched_state.vec_inames
            if iname in useful_loops_set
            ])

        # }}}

        if debug_mode:
            print("useful inames: %s" % ",".join(useful_loops_set))

        for tier in priority_tiers:
            found_viable_schedule = False

            for iname in sorted(tier,
                    key=lambda iname: iname_to_usefulness.get(iname, 0),
                    reverse=True):
                new_schedule = schedule + [EnterLoop(iname=iname)]

                for sub_sched in generate_loop_schedules_internal(
                        sched_state, loop_priority, new_schedule,
                        allow_boost=rec_allow_boost,
                        debug=debug):
                    found_viable_schedule = True
                    yield sub_sched

            if found_viable_schedule:
                return

    # }}}

    if debug_mode:
        print(75*"=")
        six.moves.input("Hit Enter for next schedule:")

    if not active_inames and not unscheduled_insn_ids:
        # if done, yield result
        debug.log_success(schedule)

        yield schedule

    else:
        if not allow_insn:
            # try again with boosting allowed
            for sub_sched in generate_loop_schedules_internal(
                    sched_state, loop_priority, schedule=schedule,
                    allow_boost=allow_boost, debug=debug,
                    allow_insn=True):
                yield sub_sched

        if not allow_boost and allow_boost is not None:
            # try again with boosting allowed
            for sub_sched in generate_loop_schedules_internal(
                    sched_state, loop_priority, schedule=schedule,
                    allow_boost=True, debug=debug,
                    allow_insn=allow_insn):
                yield sub_sched
        else:
            # dead end
            if debug is not None:
                debug.log_dead_end(schedule)

# }}}


# {{{ barrier insertion

class DependencyRecord(Record):
    """
    .. attribute:: source

        A :class:`loopy.InstructionBase` instance.

    .. attribute:: target

        A :class:`loopy.InstructionBase` instance.

    .. attribute:: variable

        A string, the name of the variable that caused the dependency to arise.

    .. attribute:: var_kind

        "global" or "local"

    .. attribute:: is_forward

        A :class:`bool` indicating whether this is a forward or reverse
        dependency.

        In a 'forward' dependency, the target depends on the source.
        In a 'reverse' dependency, the source depends on the target.
    """

    def __init__(self, source, target, variable, var_kind, is_forward):
        Record.__init__(self,
                source=source,
                target=target,
                variable=variable,
                var_kind=var_kind,
                is_forward=is_forward)


def get_barrier_needing_dependency(kernel, target, source, reverse, var_kind):
    """If there exists a depdency between target and source and the two access
    a common variable of *var_kind* in a way that requires a barrier (essentially,
    at least one write), then the function will return a tuple
    ``(target, source, var_name)``. Otherwise, it will return *None*.

    This function finds  direct or indirect instruction dependencies, but does
    not attempt to guess dependencies that exist based on common access to
    variables.

    :arg reverse: a :class:`bool` indicating whether
        forward or reverse dependencies are sought. (see above)
    :arg var_kind: "global" or "local", the kind of variable based on which
        barrier-needing dependencies should be found.
    """

    # If target or source are insn IDs, look up the actual instructions.
    from loopy.kernel.data import InstructionBase
    if not isinstance(source, InstructionBase):
        source = kernel.id_to_insn[source]
    if not isinstance(target, InstructionBase):
        target = kernel.id_to_insn[target]

    if reverse:
        source, target = target, source

    # Check that a dependency exists.
    target_deps = kernel.recursive_insn_dep_map()[target.id]
    if source.id not in target_deps:
        return None

    if var_kind == "local":
        relevant_vars = kernel.local_var_names()
    elif var_kind == "global":
        relevant_vars = kernel.global_var_names()
    else:
        raise ValueError("unknown 'var_kind': %s" % var_kind)

    tgt_write = set(target.assignee_var_names()) & relevant_vars
    tgt_read = target.read_dependency_names() & relevant_vars

    src_write = set(source.assignee_var_names()) & relevant_vars
    src_read = source.read_dependency_names() & relevant_vars

    waw = tgt_write & src_write
    raw = tgt_read & src_write
    war = tgt_write & src_read

    for var_name in raw | war:
        return DependencyRecord(
                source=source,
                target=target,
                variable=var_name,
                var_kind=var_kind,
                is_forward=not reverse)

    if source is target:
        return None

    for var_name in waw:
        return DependencyRecord(
                source=source,
                target=target,
                variable=var_name,
                var_kind=var_kind,
                is_forward=not reverse)

    return None


def barrier_kind_more_or_equally_global(kind1, kind2):
    return (kind1 == kind2) or (kind1 == "global" and kind2 == "local")


def get_tail_starting_at_last_barrier(schedule, kind):
    result = []

    for sched_item in reversed(schedule):
        if isinstance(sched_item, Barrier):
            if barrier_kind_more_or_equally_global(sched_item.kind, kind):
                break

        elif isinstance(sched_item, RunInstruction):
            result.append(sched_item.insn_id)

        elif isinstance(sched_item, (EnterLoop, LeaveLoop)):
            pass

        else:
            raise ValueError("unexpected schedule item type '%s'"
                    % type(sched_item).__name__)

    return reversed(result)


def insn_ids_from_schedule(schedule):
    result = []
    for sched_item in reversed(schedule):
        if isinstance(sched_item, RunInstruction):
            result.append(sched_item.insn_id)

        elif isinstance(sched_item, (EnterLoop, LeaveLoop, Barrier)):
            pass

        else:
            raise ValueError("unexpected schedule item type '%s'"
                    % type(sched_item).__name__)

    return result


def insert_barriers(kernel, schedule, reverse, kind, level=0):
    """
    :arg reverse: a :class:`bool`. For ``level > 0``, this function should be
        called twice, first with ``reverse=False`` to insert barriers for
        forward dependencies, and then again with ``reverse=True`` to insert
        reverse depedencies. This order is preferable because the forward pass
        will limit the number of instructions that need to be considered as
        depedency source candidates by already inserting some number of
        barriers into *schedule*.

        Calling it with ``reverse==True and level==0` is not necessary,
        since the root of the schedule is in no loop, therefore not repeated,
        and therefore reverse dependencies don't need to be added.
    :arg kind: "local" or "global". The :attr:`Barrier.kind` to be inserted.
        Generally, this function will be called once for each kind of barrier
        at the top level, where more global barriers should be inserted first.
    :arg level: the current level of loop nesting, 0 for outermost.
    """
    result = []

    # In straight-line code, we have only 'b depends on a'-type 'forward'
    # dependencies. But a loop of the type
    #
    # for i in range(10):
    #     A
    #     B
    #
    # effectively glues multiple copies of 'A;B' one after the other:
    #
    # A
    # B
    # A
    # B
    # ...
    #
    # Now, if B depends on (i.e. is required to be textually before) A in a way
    # requiring a barrier, then we will assume that the reverse dependency exists
    # as well, i.e. a barrier between the tail end fo execution of B and the next
    # beginning of A is also needed.

    if level == 0 and reverse:
        # The global schedule is in no loop, therefore not repeated, and
        # therefore reverse dependencies don't need to be added.
        return schedule

    # a list of instruction IDs that could lead to barrier-needing dependencies.
    if reverse:
        candidates = set(get_tail_starting_at_last_barrier(schedule, kind))
    else:
        candidates = set()

    past_first_barrier = [False]

    def seen_barrier():
        past_first_barrier[0] = True

        # We've just gone across a barrier, so anything that needed
        # one from above just got one.

        candidates.clear()

    def issue_barrier(dep):
        seen_barrier()

        comment = None
        if dep is not None:
            if dep.is_forward:
                comment = "for %s (%s depends on %s)" % (
                        dep.variable, dep.target.id, dep.source.id)
            else:
                comment = "for %s (%s rev-depends on %s)" % (
                        dep.variable, dep.source.id, dep.target.id)

        result.append(Barrier(comment=comment, kind=dep.var_kind))

    i = 0
    while i < len(schedule):
        sched_item = schedule[i]

        if isinstance(sched_item, EnterLoop):
            # {{{ recurse for nested loop

            subloop, new_i = gather_schedule_subloop(schedule, i)
            i = new_i

            # Run barrier insertion for inner loop
            subresult = subloop[1:-1]
            for sub_reverse in [False, True]:
                subresult = insert_barriers(
                        kernel, subresult,
                        reverse=sub_reverse, kind=kind,
                        level=level+1)

            # {{{ find barriers in loop body

            first_barrier_index = None
            last_barrier_index = None

            for j, sub_sched_item in enumerate(subresult):
                if (isinstance(sub_sched_item, Barrier) and
                        barrier_kind_more_or_equally_global(
                            sub_sched_item.kind, kind)):

                    seen_barrier()
                    last_barrier_index = j
                    if first_barrier_index is None:
                        first_barrier_index = j

            # }}}

            # {{{ check if a barrier is needed before the loop

            # (for leading (before-first-barrier) bit of loop body)
            for insn_id in insn_ids_from_schedule(subresult[:first_barrier_index]):
                search_set = candidates
                if not reverse:
                    # can limit search set in case of forward dep
                    search_set = search_set \
                            & kernel.recursive_insn_dep_map()[insn_id]

                for dep_src_insn_id in search_set:
                    dep = get_barrier_needing_dependency(
                            kernel,
                            target=insn_id,
                            source=dep_src_insn_id,
                            reverse=reverse, var_kind=kind)
                    if dep:
                        issue_barrier(dep=dep)
                        break

            # }}}

            # add trailing end (past-last-barrier) of loop body to candidates
            if last_barrier_index is None:
                candidates.update(insn_ids_from_schedule(subresult))
            else:
                candidates.update(
                        insn_ids_from_schedule(
                            subresult[last_barrier_index+1:]))

            result.append(subloop[0])
            result.extend(subresult)
            result.append(subloop[-1])

            # }}}

        elif isinstance(sched_item, Barrier):
            i += 1

            if barrier_kind_more_or_equally_global(sched_item.kind, kind):
                seen_barrier()

            result.append(sched_item)

        elif isinstance(sched_item, RunInstruction):
            i += 1

            search_set = candidates
            if not reverse:
                # can limit search set in case of forward dep
                search_set = search_set \
                        & kernel.recursive_insn_dep_map()[sched_item.insn_id]

            for dep_src_insn_id in search_set:
                dep = get_barrier_needing_dependency(
                        kernel,
                        target=sched_item.insn_id,
                        source=dep_src_insn_id,
                        reverse=reverse, var_kind=kind)
                if dep:
                    issue_barrier(dep=dep)
                    break

            result.append(sched_item)
            candidates.add(sched_item.insn_id)

        else:
            raise ValueError("unexpected schedule item type '%s'"
                    % type(sched_item).__name__)

        if past_first_barrier[0] and reverse:
            # We can quit here, because we're only trying add
            # reverse-dep barriers to the beginning of the loop, up to
            # the first barrier.

            result.extend(schedule[i:])
            break

    return result

# }}}


# {{{ main scheduling entrypoint

def generate_loop_schedules(kernel, debug_args={}):
    from loopy.kernel import kernel_state
    if kernel.state != kernel_state.PREPROCESSED:
        raise LoopyError("cannot schedule a kernel that has not been "
                "preprocessed")

    loop_priority = kernel.loop_priority

    from loopy.check import pre_schedule_checks
    pre_schedule_checks(kernel)

    schedule_count = 0

    debug = ScheduleDebugger(**debug_args)

    from loopy.kernel.data import IlpBaseTag, ParallelTag, VectorizeTag
    ilp_inames = set(
            iname
            for iname in kernel.all_inames()
            if isinstance(kernel.iname_to_tag.get(iname), IlpBaseTag))
    vec_inames = set(
            iname
            for iname in kernel.all_inames()
            if isinstance(kernel.iname_to_tag.get(iname), VectorizeTag))
    parallel_inames = set(
            iname for iname in kernel.all_inames()
            if isinstance(kernel.iname_to_tag.get(iname), ParallelTag))

    sched_state = SchedulerState(
            kernel=kernel,
            loop_nest_map=loop_nest_map(kernel),
            breakable_inames=ilp_inames,
            ilp_inames=ilp_inames,
            vec_inames=vec_inames,
            # ilp and vec are not parallel for the purposes of the scheduler
            parallel_inames=parallel_inames - ilp_inames - vec_inames)

    generators = [
            generate_loop_schedules_internal(sched_state, loop_priority,
                debug=debug, allow_boost=None),
            generate_loop_schedules_internal(sched_state, loop_priority,
                debug=debug)]
    for gen in generators:
        for gen_sched in gen:
            # gen_sched = insert_barriers(kernel, gen_sched,
            #         reverse=False, kind="global")

            # for sched_item in gen_sched:
            #     if isinstance(sched_item, Barrier) and sched_item.kind == "global":
            #         raise LoopyError("kernel requires a global barrier %s"
            #                 % sched_item.comment)

            gen_sched = insert_barriers(kernel, gen_sched,
                    reverse=False, kind="local")

            debug.stop()
            yield kernel.copy(
                    schedule=gen_sched,
                    state=kernel_state.SCHEDULED)
            debug.start()

            schedule_count += 1

        # if no-boost mode yielded a viable schedule, stop now
        if schedule_count:
            break

    debug.done_scheduling()

    if not schedule_count:
        if debug.interactive:
            print(75*"-")
            print("ERROR: Sorry--loo.py did not find a schedule for your kernel.")
            print(75*"-")
            print("Loo.py will now show you the scheduler state at the point")
            print("where the longest (dead-end) schedule was generated, in the")
            print("the hope that some of this makes sense and helps you find")
            print("the issue.")
            print()
            print("To disable this interactive behavior, pass")
            print("  debug_args=dict(interactive=False)")
            print("to generate_loop_schedules().")
            print(75*"-")
            six.moves.input("Enter:")
            print()
            print()

            debug.debug_length = len(debug.longest_rejected_schedule)
            for _ in generate_loop_schedules_internal(sched_state, loop_priority,
                    debug=debug):
                pass

        raise RuntimeError("no valid schedules found")

    logger.info("%s: schedule done" % kernel.name)

# }}}


schedule_cache = PersistentDict("loopy-schedule-cache-v4-"+DATA_MODEL_VERSION,
        key_builder=LoopyKeyBuilder())


def get_one_scheduled_kernel(kernel):
    from loopy import CACHING_ENABLED

    sched_cache_key = kernel
    from_cache = False

    if CACHING_ENABLED:
        try:
            result, ambiguous = schedule_cache[sched_cache_key]

            logger.info("%s: schedule cache hit" % kernel.name)
            from_cache = True
        except KeyError:
            pass

    if not from_cache:
        ambiguous = False

        kernel_count = 0

        from time import time
        start_time = time()

        logger.info("%s: schedule start" % kernel.name)

        for scheduled_kernel in generate_loop_schedules(kernel):
            kernel_count += 1

            if kernel_count == 1:
                # use the first schedule
                result = scheduled_kernel

            if kernel_count == 2:
                ambiguous = True
                break

        logger.info("%s: scheduling done after %.2f s" % (
            kernel.name, time()-start_time))

    if ambiguous:
        from warnings import warn
        from loopy.diagnostic import LoopyWarning
        warn("kernel scheduling was ambiguous--more than one "
                "schedule found, ignoring", LoopyWarning,
                stacklevel=2)

    if CACHING_ENABLED and not from_cache:
        schedule_cache[sched_cache_key] = result, ambiguous

    return result


# vim: foldmethod=marker
