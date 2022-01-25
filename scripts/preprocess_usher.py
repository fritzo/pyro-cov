# Copyright Contributors to the Pyro-Cov project.
# SPDX-License-Identifier: Apache-2.0

import argparse
import datetime
import logging
import math
import pickle
import re
from collections import Counter, defaultdict

import pandas as pd
import torch
import tqdm
from Bio.Phylo.NewickIO import Parser

from pyrocov.external.usher import parsimony_pb2
from pyrocov.geo import get_canonical_location_generator
from pyrocov.mutrans import START_DATE
from pyrocov.sarscov2 import nuc_mutations_to_aa_mutations
from pyrocov.usher import (
    FineToMeso,
    load_mutation_tree,
    prune_mutation_tree,
    refine_mutation_tree,
)

logger = logging.getLogger(__name__)
logging.basicConfig(format="%(relativeCreated) 9d %(message)s", level=logging.INFO)

DATE_FORMATS = {7: "%Y-%m", 10: "%Y-%m-%d"}


def try_parse_date(string):
    fmt = DATE_FORMATS.get(len(string))
    if fmt is not None:
        return datetime.datetime.strptime(string, fmt)


def try_parse_genbank(strain):
    match = re.search(r"([A-Z]+[0-9]+)\.[0-9]", strain)
    if match:
        return match.group(1)


def load_nextstrain_metadata(args, stats):
    """
    Returns a dict of dictionaries from genbank_accession to metadata.
    """
    logger.info("Loading nextstrain metadata")
    get_canonical_location = get_canonical_location_generator(
        args.recover_missing_usa_state
    )
    df = pd.read_csv("results/nextstrain/metadata.tsv", sep="\t", dtype=str)
    result = defaultdict(dict)
    for row in tqdm.tqdm(df.itertuples(), total=len(df)):
        # Collect background mutation statistics for dN/dS etc.
        for key in ["aaSubstitutions", "insertions", "substitutions"]:
            values = getattr(row, key)
            if isinstance(values, str) and values:
                stats[key].update(values.split(","))

        # Key on genbank accession.
        key = row.genbank_accession
        if not isinstance(key, str) or not key:
            continue

        # Extract dates.
        date = row.date
        if isinstance(date, str) and date and date != "?":
            date = try_parse_date(date)
            if date is not None:
                if date < args.start_date:
                    date = args.start_date  # Clip rows before start date.
                result["day"][key] = (date - args.start_date).days

        # Extract a standard location.
        location = get_canonical_location(
            row.strain, row.region, row.country, row.division, row.location
        )
        if location is not None:
            result["location"][key] = location

        lineage = row.pango_lineage
        if isinstance(lineage, str) and lineage and lineage != "?":
            result["lineage"][key] = lineage

    logger.info("Found metadata:\n{}".format({k: len(v) for k, v in result.items()}))
    return result


# These are merely fallback locations in case nextrain is missing genbank ids.
USHER_LOCATIONS = {
    "England": "Europe / United Kingdom / England",
    "Wales": "Europe / United Kingdom / Wales",
    "Scotland": "Europe / United Kingdom / Scotland",
    "Northern": "Europe / United Kingdom / Northern Ireland",
    "NorthernIreland": "Europe / United Kingdom / Northern Ireland",
}


def load_usher_metadata(args):
    """
    Returns a dict of dictionaries from usher strain id to metadata.
    """
    logger.info("Loading usher metadata")
    df = pd.read_csv("results/usher/metadata.tsv", sep="\t", dtype=str)
    result = defaultdict(dict)
    for row in tqdm.tqdm(df.itertuples(), total=len(df)):
        # Key on usher strain.
        key = row.strain
        assert isinstance(key, str) and key

        # Extract dates.
        date = row.date
        if isinstance(date, str) and date and date != "?":
            date = try_parse_date(date)
            if date is not None:
                if date < args.start_date:
                    date = args.start_date  # Clip rows before start date.
                result["day"][key] = (date - args.start_date).days

        # Extract a standard location.
        if isinstance(row.strain, str) and row.strain:
            prefix = re.split(r"[/|_]", row.strain)[0]
            location = USHER_LOCATIONS.get(prefix)
            if location is not None:
                result["location"][key] = location

        lineage = row.pangolin_lineage
        if isinstance(lineage, str) and lineage and lineage != "?":
            result["lineage"][key] = lineage

    logger.info("Found metadata:\n{}".format({k: len(v) for k, v in result.items()}))
    return result


def load_metadata(args):
    # Load metadata.
    stats = defaultdict(Counter)
    usher_metadata = load_usher_metadata(args)  # keyed on strain
    nextstrain_metadata = load_nextstrain_metadata(args, stats)  # keyed on genbank

    # Load usher tree.
    # Collect all names appearing in the usher tree.
    logger.info("Loading fine tree")
    with open(args.tree_file_out, "rb") as f:
        proto = parsimony_pb2.data.FromString(f.read())
    tree = next(Parser.from_string(proto.newick).parse())

    # Collect condensed samples.
    condensed_strains = {}
    for node in proto.condensed_nodes:
        condensed_strains[node.node_name] = list(node.condensed_leaves)

    # Propagate fine clade names downward.
    node_to_clade = {}
    for clade, meta in zip(tree.find_clades(), proto.metadata):
        if meta.clade:
            node_to_clade[clade] = meta.clade
        # Propagate down to descendent clones.
        for child in clade.clades:
            node_to_clade[child] = node_to_clade[clade]

    # Collect info from each node in the tree.
    fields = "day", "location", "lineage"
    columns = defaultdict(list)
    usher_strains = set()
    skipped = stats["skipped"]
    skipped_by_day = Counter()
    nodename_to_count = Counter()
    for node, meta in zip(tree.find_clades(), proto.metadata):
        strains = condensed_strains.get(node.name, [node.name])
        for strain in strains:
            if strain is None:
                continue
            usher_strains.add(strain)
            genbank = try_parse_genbank(strain)

            # Try to use nextstrain metadata; fallback to usher metadata.
            row = {
                k: nextstrain_metadata[k].get(genbank, usher_metadata[k].get(strain))
                for k in fields
            }
            if row["day"] is None:
                skipped["no date"] += 1
            if row["location"] is None:
                skipped["no location"] += 1
                skipped_by_day["no location", row["day"]] += 1

            columns["clade"].append(node_to_clade[node])
            columns["nodename"].append(strain)
            columns["genbank_accession"].append(genbank)
            for k, v in row.items():
                columns[k].append(v)
            nodename_to_count[node.name] += 1
    logger.info(f"Found {len(usher_strains)} strains in the usher tree")

    columns = dict(columns)
    assert len(set(map(len, columns.values()))) == 1, "columns have unequal length"
    assert sum(skipped.values()) < 2e6, f"suspicious skippage:\n{skipped}"
    logger.info(f"Skipped {sum(skipped.values())} nodes because:\n{skipped}")
    stats["skipped_by_day"] = skipped_by_day
    logger.info(f"Kept {len(columns['day'])} rows")

    with open("results/columns.pkl", "wb") as f:
        pickle.dump(columns, f)
    logger.info("Saved results/columns.pkl")

    with open(args.stats_file_out, "wb") as f:
        pickle.dump(stats, f)
    logger.info(f"Saved {args.stats_file_out}")

    return columns, nodename_to_count


def prune_tree(args, coarse_to_fine, nodename_to_count):
    logger.info(f"Loading fine tree {args.tree_file_out}")
    with open(args.tree_file_out, "rb") as f:
        proto = parsimony_pb2.data.FromString(f.read())
    tree = next(Parser.from_string(proto.newick).parse())

    # Add weights for leaves.
    cum_weights = {}
    mrca_weights = {}
    for clade in tree.find_clades():
        count = nodename_to_count[clade.name]
        cum_weights[clade] = count
        mrca_weights[clade] = count ** 2

    # Add weights of MRCA pairs.
    reverse_clades = list(tree.find_clades())
    reverse_clades.reverse()  # from leaves to root
    for parent in reverse_clades:
        for child in parent.clades:
            cum_weights[parent] += cum_weights[child]
        for child in parent.clades:
            mrca_weights[parent] += cum_weights[child] * (
                cum_weights[parent] - cum_weights[child]
            )
    num_samples = sum(nodename_to_count.values())
    assert cum_weights[tree.root] == num_samples
    assert sum(mrca_weights.values()) == num_samples ** 2

    # Aggregate among clones to basal representative.
    weights = defaultdict(float)
    for meta, parent in zip(reversed(proto.metadata), reverse_clades):
        for child in parent.clades:
            mrca_weights[parent] += mrca_weights.get(child, 0)
        assert isinstance(meta.clade, str)
        if meta.clade:
            weights[meta.clade] = mrca_weights.pop(parent)
    assert sum(weights.values()) == num_samples ** 2

    # To ensure pango lineages remain distinct, set their weights to infinity.
    for fine in coarse_to_fine.values():
        weights[fine] = math.inf
    assert "" not in weights

    # Prune the tree, minimizing the number of incorrect mutations.
    args.max_num_clades = max(args.max_num_clades, len(coarse_to_fine))
    pruned_tree_filename = f"results/lineageTree.{args.max_num_clades}.pb"
    meso_set = prune_mutation_tree(
        args.tree_file_out, pruned_tree_filename, args.max_num_clades, weights
    )
    assert len(meso_set) == args.max_num_clades
    return FineToMeso(meso_set), pruned_tree_filename


def main(args):
    # Extract mappings between coarse lineages and fine clades.
    coarse_proto = args.tree_file_in
    fine_proto = args.tree_file_out
    fine_to_coarse = refine_mutation_tree(coarse_proto, fine_proto)
    coarse_to_fines = defaultdict(list)
    for fine, coarse in fine_to_coarse.items():
        coarse_to_fines[coarse].append(fine)
    # Choose the basal representative.
    coarse_to_fine = {c: min(fs) for c, fs in coarse_to_fines.items()}

    # Create columns.
    columns, nodename_to_count = load_metadata(args)
    columns["lineage"] = [fine_to_coarse[f] for f in columns["clade"]]

    # Prune tree, updating data structures to use meso-scale clades.
    fine_to_meso, pruned_tree_filename = prune_tree(
        args, coarse_to_fine, nodename_to_count
    )
    fine_to_coarse = {fine_to_meso(f): c for f, c in fine_to_coarse.items()}
    coarse_to_fine = {c: fine_to_meso(f) for c, f in coarse_to_fine.items()}
    columns["clade"] = [fine_to_meso(f) for f in columns["clade"]]
    clade_set = set(columns["clade"])
    assert len(clade_set) <= args.max_num_clades
    with open(args.columns_file_out, "wb") as f:
        pickle.dump(columns, f)
    logger.info(f"Saved {args.columns_file_out}")

    # Convert from nucleotide mutations to amino acid mutations.
    nuc_mutations_by_clade = load_mutation_tree(pruned_tree_filename)
    assert nuc_mutations_by_clade
    aa_mutations_by_clade = {
        clade: nuc_mutations_to_aa_mutations(mutations)
        for clade, mutations in nuc_mutations_by_clade.items()
    }

    # Create dense aa features.
    clades = sorted(nuc_mutations_by_clade)
    clade_ids = {k: i for i, k in enumerate(clades)}
    aa_mutations = sorted(set().union(*aa_mutations_by_clade.values()))
    logger.info(f"Found {len(aa_mutations)} amino acid mutations")
    mutation_ids = {k: i for i, k in enumerate(aa_mutations)}
    aa_features = torch.zeros(len(clade_ids), len(mutation_ids), dtype=torch.bool)
    for clade, ms in aa_mutations_by_clade.items():
        i = clade_ids[clade]
        for m in ms:
            j = mutation_ids.get(m)
            aa_features[i, j] = True

    # Save features.
    features = {
        "clades": clades,
        "clade_to_lineage": fine_to_coarse,
        "lineage_to_clade": coarse_to_fine,
        "aa_mutations": aa_mutations,
        "aa_features": aa_features,
    }
    logger.info(
        f"saving {tuple(aa_features.shape)} aa features to {args.features_file_out}"
    )
    torch.save(features, args.features_file_out)
    logger.info(f"Saved {args.features_file_out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preprocess pangolin mutations")
    parser.add_argument(
        "--usher-metadata-file-in", default="results/usher/metadata.tsv"
    )
    parser.add_argument(
        "--nextstrain-metadata-file-in", default="results/nextstrain/metadata.tsv"
    )
    parser.add_argument("--tree-file-in", default="results/usher/all.masked.pb")
    parser.add_argument("--tree-file-out", default="results/lineageTree.fine.pb")
    parser.add_argument("--stats-file-out", default="results/stats.pkl")
    parser.add_argument("--columns-file-out", default="")
    parser.add_argument("--features-file-out", default="")
    parser.add_argument("--recover-missing-usa-state", action="store_true")
    parser.add_argument("-c", "--max-num-clades", type=int, default=3000)
    parser.add_argument("--start-date", default=START_DATE)
    args = parser.parse_args()
    args.start_date = try_parse_date(args.start_date)
    if not args.features_file_out:
        args.features_file_out = f"results/features.{args.max_num_clades}.1.pt"
    if not args.columns_file_out:
        args.columns_file_out = f"results/columns.{args.max_num_clades}.pkl"
    main(args)
