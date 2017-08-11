#!/usr/bin/env python

# USAGE:
#   See collapse.py -h for details
#
# DESCRIPTION:
#   Collapses reads which 1) Start at the same position 2) Are mapped onto the same strand, and
#   3) Contain the same (or very similar) adapter sequences into a consensus sequenc
#
# AUTHORS
#   Creator: Marco Albuquerque
#   Christopher Rushton (ckrushto@sfu.ca)

# If ProDuSe is not installed or running in python2, this works fine
try:
    import fastq
    import alignment
except ImportError:
    # If installed and running in python3
    from ProDuSe import fastq
    from ProDuSe import alignment

import configargparse
import configparser
import sys
import os
import pysam
import time

"""
Processes command line arguments using configargparse

Returns:
    args: A namespace object containing parameters passed from the command line
Raises:
    parser.error: If a supplied command line argument is incorrect or invalid

"""
desc = "Creates a strand specific consensus sequence using reads provided in the BAM file."
parser = configargparse.ArgParser(description=desc)

# Processes arguments
parser.add(
    "-c", "--config",
    required=False,
    is_config_file=True,
    type=lambda x: is_valid_file(x, parser),
    help="Optional configuration file, which can provide any of the input arguments."
    )

parser.add(
    "-i", "--input",
    metavar="BAM",
    type=lambda x: is_valid_file(x, parser),
    required=True,
    help="An input bam file for collapsing, generated by the ProDuSe pipeline using trim.py and bwa.py"
    )

parser.add(
    "-r", "--reference",
    metavar="FASTA",
    type=lambda x: is_valid_file(x, parser),
    required=True,
    help="Reference genome, in FASTA format"
    )

parser.add(
    "-o", "--output",
    metavar="FASTQ",
    required=True,
    action="append",
    help="Output fastq files, listing the consensus forward and reverse reads"
    )

parser.add(
    "-sp", "--strand_position",
    metavar="STR",
    type=str,
    required=True,
    help="The positions in the adapter sequence to include in distance calculations, 0 for no, 1 for yes."
    )

parser.add(
    "-dp", "--duplex_position",
    metavar="STR",
    type=str,
    required=True,
    help="The positions in the adapter sequence to include in distance calculations between forward and reverse reads, 0 for no, 1 for yes"
    )

# Used to maintain backwards compatibility with the poorly-named strand mis-match
adapterMismatch = parser.add_mutually_exclusive_group(required=True)
adapterMismatch.add(
    "-amm", "--adapter_max_mismatch",
    type=int,
    help="The maximum number of mismatches allowed between the expected and actual adapter sequences [Default: %(default)s]",
    )
adapterMismatch.add(
    "--strand_max_mismatch",
    type=int,
    help=configargparse.SUPPRESS,
)

parser.add(
    "-dmm", "--duplex_max_mismatch",
    type=int,
    required=True,
    help="The maximum number of mismatches allowed between the expected and actual duplexed adapter sequences",
    )

parser.add(
    "-smm", "--sequence_max_mismatch",
    type=int,
    required=False,
    default=20,
    help="The maximum number of mismatches allowed in an alignment"
    )

parser.add(
    "-as", "--adapter_sequence",
    type=str,
    required=False,
    help="The semi-degenerate adapter sequence, described using IUPAC bases. Used to identify chimeric adapters"
)

parser.add(
    "--discard_chimeric_sequences",
    action="store_true",
    default=False,
    help="Discard chimeric reads")

parser.add(
    "-oo", "--original_output",
    type=str,
    required=False,
    action="append",
    nargs=2,
    default=None,
    help="A pair of empty fastq files to rewrite original fastqs with duplex information"
    )

parser.add(
    "-fp", "--family_plot",
    type=str,
    required=False,
    default=None,
    help="A histogram to plot molecule counts per read family (i.e. each consensus read)"
    )


def is_valid_file(file, parser):
    """
    Checks to ensure the specified file exists, and throws an error if it does not

    Args:
        file: A filepath
        parser: An argparse.ArgumentParser() object. Used to throw an exception if the file does not exist

    Returns:
        type: The file itself

    Raises:
        parser.error(): An ArguParser.error() object, thrown if the file does not exist
    """

    if os.path.isfile(file):
        return file
    else:
        parser.error("Unable to find %s" % (file))


def plot_molecule_families(counts_per_family, plot_file):
    """
    Generates a histograph for molecule counts per family

    Args:
        counts_per_family: A dictionary listing molecule_counts:Abundance
        plot_file: Output file for the histograph
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from astropy.visualization import hist

    hist(counts_per_family, bins="scott")
    plt.xlabel("Molecules per Read Class")
    plt.ylabel("Abundance")
    plt.title("Distribution of molecule families")
    plt.savefig(plot_file)
    plt.close()

def main(args=None):

    """
    Creates a consensus sequences for duplicate reads

    Using the adapter sequences, reads which start at the same position and have very similar adapter
    sequences are merged into a single consensus sequence. This is performed for both the forward and
    reverse reads. The original fastq files can also be modified to list read pairing
    information

    Args:
        args: A namespace object listing input files, output files, and paramters.
                See get_args() for more info
    """

    if args is None:
        args = parser.parse_args()
    elif args.config:

        # Since configargparse does not parse commands from the config file if they are passed as argument here
        # They must be parsed manually
        cmdArgs = vars(args)
        config = configparser.ConfigParser()
        config.read(args.config)
        configOptions = config.options("config")
        for option in configOptions:
            param = config.get("config", option)
            # Convert arguments that are lists into an actual list
            if param[0] == "[" and param[-1] == "]":
                param = param[1:-1].split(",")

            # WARNING: Command line arguments will be SUPERSCEEDED BY CONFIG FILE ARGUMENTS
            cmdArgs[option] = param

    if not args.adapter_max_mismatch:
        args.adapter_max_mismatch = args.strand_max_mismatch

    # Checks command line arguments
    if not len(args.output) == 2:
        parser.error('--output must be specified exactly twice (i.e. -o file1.fastq -o file2.fastq)')

    if len(args.duplex_position) != len(args.strand_position):
        parser.error("duplex_position and strand_position must have same length")

    if os.path.isfile(args.output[0]):
        parser.error("Output file %s already exists" % args.output[0])

    if os.path.isfile(args.output[1]):
        parser.error("Output file %s already exists" % args.output[1])

    if args.original_output is not None:
        if os.path.isfile(args.original_output[0]):
            parser.error("Original output file %s already exist" % args.original_output[0])

        if os.path.isfile(args.original_output[1]):
            parser.error("Original output file %s already exist" % args.original_output[1])

    if args.discard_chimeric_sequences and not args.adapter_sequence:
        parser.error("If --disarc_chimeric_sequences is specified, an --adapter_sequence must also be provided")

    # Sets output file types for the standard output fastqs
    outOneType = 'w'
    outTwoType = "w"
    output_one_gzipped = args.output[0].endswith(".gz")
    output_two_gzipped = args.output[1].endswith(".gz")
    if output_one_gzipped:
        outOneType = ''.join([outOneType, 'g'])
    if output_two_gzipped:
        outTwoType = ''.join([outTwoType, 'g'])
    # Opens standard output fastq files
    forward_fastq = fastq.FastqOpen(args.output[0], outOneType)
    reverse_fastq = fastq.FastqOpen(args.output[1], outTwoType)

    # If the original output fastqs were specified, set the output type
    if args.original_output is not None:
        origOutOneType = 'w'
        origOutTwoType = 'w'
        output_one_gzipped = args.original_output[0].endswith(".gz")
        output_two_gzipped = args.original_output[1].endswith(".gz")
        if output_one_gzipped:
            origOutOneType = ''.join([origOutOneType, 'g'])
        if output_two_gzipped:
            origOutTwoType = ''.join([origOutTwoType, 'g'])
    # If specified, opens original output fastq files
    original_forward_fastq = None
    original_reverse_fastq = None
    if args.original_output is not None:
        original_forward_fastq = fastq.FastqOpen(args.original_output[0], origOutOneType)
        original_reverse_fastq = fastq.FastqOpen(args.original_output[1], origOutTwoType)

    print_prefix = "PRODUSE-COLLAPSE"
    sys.stderr.write("\t".join([print_prefix, time.strftime('%X'), "Starting...\n"]))

    # Load up BAM file and Ref Genome
    bamfile = pysam.AlignmentFile(args.input, 'rb')
    fasta_file = pysam.FastaFile(args.reference)

    # Sets positions for foward and reverse reads
    strand_indexes = list(''.join([args.strand_position, args.strand_position]))
    strand_indexes = [i for i in range(len(strand_indexes)) if strand_indexes[i] == "1"]
    adapter_sequence = "".join([args.adapter_sequence,args.adapter_sequence])
    duplex_indexes = list(''.join([args.duplex_position, args.duplex_position]))
    duplex_indexes = [i for i in range(len(duplex_indexes)) if duplex_indexes[i] == "1"]

    # Loads up reads from the BAM file, and group them based upon start position
    collection_creator = alignment.AlignmentCollectionCreate(bamfile, ref_genome=fasta_file,
                                                            max_alignment_mismatch_threshold=int(args.sequence_max_mismatch),
                                                            adapter_sequence=args.adapter_sequence,
                                                            adapter_position=[i for i in range(len(args.strand_position)) if strand_indexes[i] == "1"],
                                                            discard_chimers=args.discard_chimeric_sequences, adapter_max_mismatch=int(args.adapter_max_mismatch))
    counter = 0
    collapsed_reads = 0
    family_abundances = []
    for collection in collection_creator:
        counter += 1

        # All the magic occurs in here.
        collection.adapter_table_average_consensus(
            forward_fastq=forward_fastq,
            reverse_fastq=reverse_fastq,
            strand_mismatch=int(args.adapter_max_mismatch),
            strand_indexes=strand_indexes,
            duplex_mismatch=int(args.duplex_max_mismatch),
            duplex_indexes=duplex_indexes,
            original_forward_fastq=original_forward_fastq,
            original_reverse_fastq=original_reverse_fastq)

        collapsed_reads += (collection.length - 1) * 2

        # For generating a histogram of molecule class abundances
        family_abundances.append(collection.length)

        # Prints a status update to the command line
        if counter % 100000 == 0:
            sys.stderr.write("\t".join([print_prefix, time.strftime('%X'), "Positions Processed:" + str(counter) + "\n"]))

    if args.family_plot:
        plot_molecule_families(family_abundances, args.family_plot)

    sys.stderr.write("\t".join([print_prefix, time.strftime('%X'), "Positions Processed:" + str(counter) + "\n"]))
    sys.stderr.write("\t".join([print_prefix, time.strftime('%X'), "Total Reads Collapsed:" + str(collapsed_reads) + "\n"]))

if __name__ == "__main__":

    main()
