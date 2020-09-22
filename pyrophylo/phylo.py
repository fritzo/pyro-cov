import math

import pyro.distributions as dist
import torch
from pyro.distributions import constraints, is_validation_enabled


class Phylogeny:
    """
    Tensor data structure to represent a (batched) phylogenetic tree.

    The tree is timed and is assumed to have only binary nodes; polysemy is
    represented as multiple binary nodes but with zero branch length.

    :param Tensor times: float tensor of times of each node. Must be ordered.
    :param Tensor parents: int tensor of parent id of each node. The root node
        must be first and have null id ``-1``.
    :param Tensor leaves: int tensor of ids of all leaf nodes.
    """
    _fields = ("times", "parents", "leaves")

    def __init__(self, times, parents, leaves):
        num_nodes = times.size(-1)
        assert num_nodes % 2 == 1, "expected odd number of nodes"
        num_leaves = (num_nodes + 1) // 2
        assert parents.shape == times.shape
        assert leaves.shape == times.shape[:-1] + (num_leaves,)
        assert (times[..., :-1] <= times[..., 1:]).all(), "expected nodes ordered by time"
        assert (parents[..., 0] == -1).all(), "expected root node first"
        if __debug__:
            _parents = parents[..., 1:]
            is_leaf_1 = torch.ones_like(parents, dtype=torch.bool)
            is_leaf_1.scatter_(-1, _parents, is_leaf_1.new_zeros(_parents.shape))
            is_leaf_2 = torch.zeros_like(is_leaf_1)
            is_leaf_2.scatter_(-1, leaves, is_leaf_2.new_ones(leaves.shape))
            assert (is_leaf_1.sum(-1) == num_leaves).all()
            assert (is_leaf_2.sum(-1) == num_leaves).all()
            assert (is_leaf_2 == is_leaf_1).all()
        super().__init__()
        self.times = times
        self.parents = parents
        self.leaves = leaves

    @property
    def num_nodes(self):
        return self.times.size(-1)

    @property
    def num_leaves(self):
        return self.leaves.size(-1)

    @property
    def batch_shape(self):
        return self.times.shape[:-1]

    def __len__(self):
        return self.batch_shape[0]

    def __getitem__(self, index):
        kwargs = {name: getattr(self, name)[index] for name in self._fields}
        return Phylogeny(**kwargs)

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]

    def contiguous(self):
        kwargs = {name: getattr(self, name).contiguous() for name in self._fields}
        return Phylogeny(**kwargs)

    def num_lineages(self):
        _parents = self.parents[..., 1:]
        sign = torch.ones_like(self.parents)
        sign.scatter_(-1, _parents, sign.new_full(_parents.shape, -1.))
        num_lineages = sign.flip(-1).cumsum(-1).flip(-1)
        return num_lineages

    @staticmethod
    def stack(phylogenies):
        """
        :param iterable phylogenies: An iterable of :class:`Phylogeny` objects
            of identical shape.
        :returns: A batched phylogeny.
        :rtype: Phylogeny
        """
        phylogenies = list(phylogenies)
        kwargs = {name: torch.stack([getattr(x, name) for x in phylogenies])
                  for name in Phylogeny._fields}
        return Phylogeny(**kwargs)

    @staticmethod
    def from_bio_phylo(tree):
        """
        Builds a :class:`Phylogeny` object from a biopython tree structure.

        :param Bio.Phylo.BaseTree.Clade tree: A phylogenetic tree.
        :returns: A single phylogeny.
        :rtype: Phylogeny
        """
        # Compute time as cumulative branch length.
        def get_branch_length(clade):
            branch_length = clade.branch_length
            return 1.0 if branch_length is None else branch_length

        # Collect times and parents.
        clades = list(tree.find_clades())
        clade_to_time = {tree.root: get_branch_length(tree.root)}
        clade_to_parent = {}
        for clade in clades:
            time = clade_to_time[clade]
            for child in clade:
                clade_to_time[child] = time + get_branch_length(child)
                clade_to_parent[child] = clade
        clades.sort(key=lambda c: (clade_to_time[c], c.name))
        assert clades[0] not in clade_to_parent, "invalid root"
        # TODO binarize the tree
        clade_to_id = {clade: i for i, clade in enumerate(clades)}
        times = torch.tensor([float(clade_to_time[clade]) for clade in clades])
        parents = torch.tensor([-1] + [clade_to_id[clade_to_parent[clade]]
                                       for clade in clades[1:]])

        # Construct leaf index ordered by clade.name.
        leaves = [clade for clade in clades if len(clade) == 0]
        leaves.sort(key=lambda clade: clade.name)
        leaves = torch.tensor([clade_to_id[clade] for clade in leaves])

        return Phylogeny(times, parents, leaves)


def markov_log_prob(phylo, leaf_state, state_trans):
    """
    Compute the marginal log probability of a Markov tree with given edge
    transitions and leaf observations. This can be used for either mutation or
    phylogeographic mugration models, but does not allow state-dependent
    reproduction rate as in the structured coalescent [1].

    **References**

    [1] T. Vaughan, D. Kuhnert, A. Popinga, D. Welch, A. Drummond (2014)
        `Efficient Bayesian inference under the structured coalescent`
        https://academic.oup.com/bioinformatics/article/30/16/2272/2748160

    :param Phylogeny phylo: A phylogeny or batch of phylogenies.
    :param Tensor leaf_state: int tensor of states of all leaf nodes.
    :param Tensor state_trans: Either a homogeneous reverse-time state
        transition matrix, or a heterogeneous grid of ``T`` transition matrices
        applying to time intervals ``(-inf,1]``, ``(1,2]``, ..., ``(T-1,inf)``.
    :returns: Marginal log probability of data ``leaf_state``.
    :rtype: Tensor
    """
    batch_shape = phylo.batch_shape
    if batch_shape:
        # TODO vectorize.
        return torch.stack([markov_log_prob(p, leaf_state, state_trans)
                            for p in phylo])
    num_nodes = phylo.num_nodes
    num_leaves = phylo.num_leaves
    num_states = state_trans.size(-1)
    assert leaf_state.shape == (num_leaves,)
    assert state_trans.dim() in (2, 3)  # homogeneous, heterogeneous
    assert state_trans.shape[-2:] == (num_states, num_states)
    if is_validation_enabled():
        constraints.simplex.check(state_trans.exp())
    times = phylo.times
    parents = phylo.parents
    leaves = phylo.leaves

    # Convert (leaves,leaf_state) to initial state log density.
    logp = state_trans.new_zeros(num_nodes, num_states)
    logp[leaves] = -math.inf
    logp[leaves, leaf_state] = 0

    # Dynamic programming along the tree.
    for i in range(-1, -num_nodes, -1):
        j = parents[i]
        logp[j] += _interpolate_lmve(times[j], times[i], state_trans, logp[i])
    logp = logp[0].logsumexp(-1)
    return logp


class MarkovTree(dist.TorchDistribution):
    """
    :param Phylogeny phylo: A phylogeny or batch of phylogenies.
    :param Tensor state_trans: Either a homogeneous reverse-time state
        transition matrix, or a heterogeneous grid of ``T`` transition matrices
        applying to time intervals ``(-inf,1]``, ``(1,2]``, ..., ``(T-1,inf)``.
    """
    arg_constraints = {
        "transition": constraints.IndependentConstraint(constraints.simplex, 2),
    }

    def __init__(self, phylogeny, transition):
        assert isinstance(transition, torch.Tensor)
        assert isinstance(phylogeny, Phylogeny)
        assert transition.dim() in (2, 3)
        self.num_states = transition.size(-1)
        assert transition.size(-2) == self.num_states
        self.phylogeny = phylogeny
        self.transition = transition
        batch_shape = phylogeny.batch_shape
        event_shape = torch.Size([phylogeny.num_leaves])
        super().__init__(batch_shape, event_shape)

    @constraints.dependent_property
    def support(self):
        return constraints.IndependentConstraint(
            constraints.integer_interval(0, self.num_states), 1)

    def log_prob(self, leaf_state):
        return markov_log_prob(self.phylogeny, leaf_state, self.transition)


def _lmve(matrix, log_vector):
    """
    log(matrix @ exp(log_vector))
    """
    shift = log_vector.logsumexp(-1, keepdim=True)
    p = (log_vector - shift).exp()
    q = matrix.matmul(p.unsqueeze(-1)).squeeze(-1)
    return q.log() + shift


def _interpolate_lmve(t0, t1, matrix, log_vector):
    if is_validation_enabled():
        assert (t0 <= t1).all()
    if matrix.dim() == 2 or matrix.size(-3) == 1:
        # homogeneous
        m = matrix.matrix_power(t1 - t0)
        return _lmve(m, log_vector)
    # heterogeneous
    assert t0.numel() == 1 and t1.numel() == 1
    raise NotImplementedError("TODO support time-inhomogeneous transitions")
