from collections import Counter
from itertools import groupby

import sympy

from devito.ir.support import Any, Backward, Forward, IterationSpace, Scope
from devito.ir.clusters.cluster import Cluster, ClusterGroup
from devito.symbolics import CondEq
from devito.tools import DAG, as_tuple, flatten

__all__ = ['clusterize', 'optimize']


def clusterize(exprs):
    """
    Turn a sequence of LoweredEqs into a sequence of Clusters.
    """
    # Initialization
    clusters = [Cluster(e, e.ispace, e.dspace) for e in exprs]

    # Enforce iteration directions
    clusters = Queue(enforce).process(clusters)

    # Compute a topological ordering that honours flow- and anti-dependences
    # Note: heuristically (see toposort.choose_element) this tries to maximize
    # loop fusion
    cgroups = [ClusterGroup(c, c.itintervals) for c in clusters]
    cgroups = Queue(toposort, aggregate).process(cgroups)
    clusters = ClusterGroup.concatenate(*cgroups)

    # Apply optimizations
    clusters = optimize(clusters)

    # Introduce conditional Clusters
    clusters = guard(clusters)

    return ClusterGroup(clusters)


class Queue(object):

    """
    A special queue to process objects in nested IterationSpaces based on
    a divide-and-conquer algorithm.

    Parameters
    ----------
    callbacks : list of callables, optional
        Routines executed upon conquer.
    """

    def __init__(self, *callbacks):
        self.callbacks = as_tuple(callbacks)

    def _process(self, elements, level, prefix=None):
        prefix = prefix or []
        # Divide part
        processed = []
        for pfx, g in groupby(elements, key=lambda i: i.itintervals[:level]):
            if level > len(pfx):
                # Base case
                processed.extend(list(g))
            else:
                # Recursion
                processed.extend(self._process(list(g), level + 1, pfx))
        # Conquer part (execute callbacks)
        for f in self.callbacks:
            processed = f(processed, prefix)
        return processed

    def process(self, elements):
        return self._process(elements, 1)


def build_dag(cgroups, prefix):
    """
    A DAG capturing dependences between *all* ClusterGroups within an iteration space.

    Examples
    --------
    When do we need to sequentialize two ClusterGroup `cg0` and `cg1`?
    Essentially any time there's a dependence between them, apart from when it's
    a carried flow-dependence within the given iteration space.

    Let's consider two ClusterGroups `cg0` and `cg1` within the iteration space
    identified by the Dimension `i`.

    1) cg0 := b[i, j] = ...
       cg1 := ... = ... b[i, j] ...
       Non-carried flow-dependence, so `cg1` must go after `cg0`

    2) cg0 := b[i, j] = ...
       cg1 := ... = ... b[i, j+1] ...
       Anti-dependence in `j`, so `cg1` must go after `cg0`

    3) cg0 := b[i, j] = ...
       cg1 := ... = ... b[i-1, j+1] ...
       Flow-dependence in `i`, so `cg1` can safely go before or after `cg0`
       (but clearly still within the `i` iteration space).
       Note: the `j+1` in `cg1` has no impact -- the dependence is in `i`.

    4) cg0 := b[i, j] = ...
       cg1 := ... = ... b[i, j-1] ...
       Flow-dependence in `j`, so `cg1` must go after `cg0`.
       Unlike case 3), the flow-dependence is along an inner Dimension, so
       `cg0` and `cg1 need to be sequentialized.
    """
    prefix = {i.dim for i in as_tuple(prefix)}

    dag = DAG(nodes=cgroups)
    for i, cg0 in enumerate(cgroups):
        for cg1 in cgroups[i+1:]:
            scope = Scope(exprs=cg0.exprs + cg1.exprs)

            # Handle anti-dependences
            local_deps = cg0.scope.d_anti + cg1.scope.d_anti
            if scope.d_anti - local_deps:
                dag.add_edge(cg0, cg1)
                break

            # Flow-dependences along one of the `prefix` Dimensions can
            # be ignored; all others require sequentialization
            local_deps = cg0.scope.d_flow + cg1.scope.d_flow
            if any(dep.cause - prefix for dep in scope.d_flow - local_deps):
                dag.add_edge(cg0, cg1)
                break

    return dag


def toposort(cgroups, prefix):
    """
    A new heuristic-based topological ordering for some ClusterGroups. The
    heuristic attempts to maximize Cluster fusion by bringing together Clusters
    with compatible IterationSpace.
    """
    # Are there any ClusterGroups that could potentially be fused? If not,
    # don't waste time computing a new topological ordering
    counter = Counter(cg.itintervals for cg in cgroups)
    if not any(v > 1 for it, v in counter.most_common()):
        return cgroups

    # Similarly, if all ClusterGroups have the same exact prefix, no need
    # to topologically resort
    if len(counter.most_common()) == 1:
        return cgroups

    dag = build_dag(cgroups, prefix)

    def choose_element(queue, scheduled):
        # Heuristic 1: do not move Clusters computing Arrays (temporaries),
        # to preserve cross-loop blocking opportunities
        # Heuristic 2: prefer a node having same IterationSpace as that of
        # the last scheduled node to maximize Cluster fusion
        if not scheduled:
            return queue.pop()
        last = scheduled[-1]
        for i in list(queue):
            if any(f.is_Array for f in i.scope.writes):
                continue
            elif i.itintervals == last.itintervals:
                queue.remove(i)
                return i
        return queue.popleft()

    processed = dag.topological_sort(choose_element)

    return processed


def aggregate(cgroups, prefix):
    """
    Concatenate a sequence of ClusterGroups into a new ClusterGroup.
    """
    return [ClusterGroup(cgroups, prefix)]


def enforce(clusters, prefix, backlog=None, known_flow_break=None):
    """
    Replace `Any` IterationDirections with either `Forward` or `Backward`
    so that the information naturally flows from one iteration to another.
    """
    if not prefix:
        return clusters

    # Take the innermost Dimension -- no other Clusters other than those in
    # `clusters` are supposed to share it
    candidates = prefix[-1].dim._defines

    scope = Scope(exprs=flatten(c.exprs for c in clusters))

    # The most nasty case:
    # eq0 := u[t+1, x] = ... u[t, x]
    # eq1 := v[t+1, x] = ... v[t, x] ... u[t-1, x] ... u[t, x] ... u[t+1, x]
    # Here, `eq0` marches forward along `t`, while `eq1` has both a flow and an
    # anti dependence with `eq0`, which ultimately will require `eq1` to go in
    # a separate t-loop
    require_flow_break = (scope.d_flow.cause & scope.d_anti.cause) & candidates
    if require_flow_break and len(clusters) > 1:
        backlog = [clusters[-1]] + (backlog or [])
        # Try with increasingly smaller Cluster groups until the ambiguity is solved
        return enforce(clusters[:-1], prefix, backlog, require_flow_break)

    # Compute iteration directions
    directions = {d: Backward for d in scope.d_anti.cause & candidates}
    directions.update({d.Forward for d in scope.d_flow.cause & candidates})
    directions.update({d: Forward for d in candidates if d not in directions})

    # Enforce iteration directions on each Cluster
    processed = []
    for c in clusters:
        ispace = IterationSpace(c.ispace.intervals, c.ispace.sub_iterators,
                                {**c.ispace.directions, **directions})
        processed.append(Cluster(c.exprs, ispace, c.dspace))

    if backlog is None:
        return processed

    # Handle the backlog -- the Clusters characterized by flow+anti dependences along
    # one or more Dimensions
    directions = {d: Any for d in known_flow_break}
    for i, c in enumerate(as_tuple(backlog)):
        ispace = IterationSpace(c.ispace.intervals.lift(known_flow_break),
                                c.ispace.sub_iterators,
                                {**c.ispace.directions, **directions})
        backlog[i] = Cluster(c.exprs, ispace, c.dspace)

    return processed + enforce(backlog, prefix)


def optimize(clusters):
    """
    Optimize a topologically-ordered sequence of Clusters by applying the
    following transformations:

        * Fusion
        * Lifting

    Notes
    -----
    This function relies on advanced data dependency analysis tools based upon classic
    Lamport theory.
    """
    # Lifting
    clusters = Queue(lift).process(clusters)

    # Fusion
    clusters = fuse(clusters)

    return clusters


def lift(clusters, prefix):
    if not prefix:
        # No iteration space to be lifted from
        return clusters

    hope_invariant = {i.dim for i in prefix}
    candidates = [c for c in clusters if
                  any(e.is_Tensor for e in c.exprs) and  # Not just scalar exprs
                  not any(e.is_Increment for e in c.exprs) and  # No reductions
                  not c.used_dimensions & hope_invariant]  # Not an invariant ispace
    if not candidates:
        return clusters

    # Now check data dependences
    lifted = []
    processed = []
    for c in clusters:
        impacted = set(clusters) - {c}
        if c in candidates and\
                not any(set(c.functions) & set(i.scope.writes) for i in impacted):
            # Perform lifting, which requires contracting the iteration space
            key = lambda d: d not in hope_invariant
            ispace = c.ispace.project(key)
            dspace = c.dspace.project(key)
            lifted.append(Cluster(c.exprs, ispace, dspace, guards=c.guards))
        else:
            processed.append(c)

    return lifted + processed


def fuse(clusters):
    """
    Fuse sub-sequences of Clusters with compatible IterationSpace.
    """
    processed = []
    for k, g in groupby(clusters, key=lambda cg: cg.itintervals):
        maybe_fusible = list(g)

        if len(maybe_fusible) == 1 or any(c.guards for c in maybe_fusible):
            processed.extend(maybe_fusible)
        else:
            # Perform fusion
            fused = Cluster.from_clusters(*maybe_fusible)
            processed.append(fused)

    return processed


def guard(clusters):
    """
    Split Clusters containing conditional expressions into separate Clusters.
    """
    processed = []
    for c in clusters:
        free = []
        for e in c.exprs:
            if e.conditionals:
                # Expressions that need no guarding are kept in a separate Cluster
                if free:
                    processed.append(Cluster(free, c.ispace, c.dspace))
                    free = []
                # Create a guarded Cluster
                guards = {}
                for d in e.conditionals:
                    condition = guards.setdefault(d.parent, [])
                    condition.append(d.condition or CondEq(d.parent % d.factor, 0))
                guards = {k: sympy.And(*v, evaluate=False) for k, v in guards.items()}
                processed.append(Cluster(e, c.ispace, c.dspace, guards))
            else:
                free.append(e)
        # Leftover
        if free:
            processed.append(Cluster(free, c.ispace, c.dspace))

    return processed
