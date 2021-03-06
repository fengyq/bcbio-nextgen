"""Run and organization variant file annotations with vcfanno.
"""
import os

import toolz as tz

from bcbio import utils
from bcbio.bam import ref
from bcbio.log import logger
from bcbio.distributed.transaction import file_transaction
from bcbio.provenance import do
from bcbio.pipeline import config_utils
import bcbio.pipeline.datadict as dd
from bcbio.variation import naming, vcfutils

def run(vcf, conf_fns, lua_fns, data, basepath=None, decomposed=False):
    """Annotate a VCF file using vcfanno (https://github.com/brentp/vcfanno)

    decomposed -- if set to true we'll convert allele based output into single values
      to match alleles and make compatible with vcf2db
      (https://github.com/quinlan-lab/vcf2db/issues/14)
    """
    conf_fns.sort(key=lambda x: os.path.basename(x) if x else x)
    lua_fns.sort(key=lambda x: os.path.basename(x) if x else x)
    ext = "-annotated-%s" % utils.splitext_plus(os.path.basename(conf_fns[0]))[0]
    if vcf.find(ext) > 0:
        out_file = vcf
    else:
        out_file = "%s%s.vcf.gz" % (utils.splitext_plus(vcf)[0], ext)
    if not utils.file_exists(out_file):
        vcfanno = config_utils.get_program("vcfanno", data)
        with file_transaction(out_file) as tx_out_file:
            conffn = _combine_files(conf_fns, out_file, data, basepath is None)
            luafn = _combine_files(lua_fns, out_file, data, False)
            luaflag = "-lua {0}".format(luafn) if luafn and utils.file_exists(luafn) else ""
            basepathflag = "-base-path {0}".format(basepath) if basepath else ""
            cores = dd.get_num_cores(data)
            post_ann = "sed -e 's/Number=A/Number=1/g' |" if decomposed else ""
            cmd = ("{vcfanno} -p {cores} {luaflag} {basepathflag} {conffn} {vcf} "
                   "| {post_ann} bgzip -c > {tx_out_file}")
            message = "Annotating {vcf} with vcfanno, using {conffn}".format(**locals())
            do.run(cmd.format(**locals()), message)
    return vcfutils.bgzip_and_index(out_file, data["config"])

def _combine_files(orig_files, base_out_file, data, fill_paths=True):
    """Combine multiple input files, fixing file paths if needed.

    We fill in full paths from files in the data dictionary if we're
    not using basepath (old style GEMINI).
    """
    orig_files = [x for x in orig_files if x and utils.file_exists(x)]
    if not orig_files:
        return None
    out_file = "%s-combine%s" % (utils.splitext_plus(base_out_file)[0],
                                    utils.splitext_plus(orig_files[0])[-1])
    with open(out_file, "w") as out_handle:
        for orig_file in orig_files:
            with open(orig_file) as in_handle:
                for line in in_handle:
                    if fill_paths and line.startswith("file"):
                        line = _fill_file_path(line, data)
                    out_handle.write(line)
            out_handle.write("\n\n")
    return out_file

def _fill_file_path(line, data):
    """Fill in a full file path in the configuration file from data dictionary.
    """
    def _find_file(xs, target):
        if isinstance(xs, dict):
            for v in xs.values():
                f = _find_file(v, target)
                if f:
                    return f
        elif isinstance(xs, (list, tuple)):
            for x in xs:
                f = _find_file(x, target)
                if f:
                    return f
        elif isinstance(xs, basestring) and os.path.exists(xs) and xs.endswith("/%s" % target):
            return xs
    orig_file = os.path.basename(line.split("=")[-1].replace('"', '').strip())
    full_file = _find_file(data, orig_file)
    assert full_file, "Did not find vcfanno input file %s" % (orig_file)
    return 'file="%s"\n' % full_file

def find_annotations(data):
    """Find annotation configuration files for vcfanno, using pre-installed inputs.

    Creates absolute paths for user specified inputs and finds locally
    installed defaults.

    Default annotations if not specified:
      - gemini for variant pipelines
      - somatic for variant tumor pipelines
      - rnaedit for RNA-seq variant calling
    """
    conf_files = dd.get_vcfanno(data)
    if not conf_files:
        conf_files = _default_conf_files(data)
    if not isinstance(conf_files, (list, tuple)):
        conf_files = [conf_files]
    out = []
    annodir = os.path.normpath(os.path.abspath(os.path.join(os.path.dirname(dd.get_ref_file(data)),
                                                            os.pardir, "config", "vcfanno")))
    for conf_file in conf_files:
        if utils.file_exists(conf_file) and os.path.isfile(conf_file):
            conffn = conf_file
        else:
            conffn = os.path.join(annodir, conf_file + ".conf")
        if not utils.file_exists(conffn):
            build = dd.get_genome_build(data)
            CONF_NOT_FOUND = (
                "The vcfanno configuration {conffn} was not found for {build}, skipping.")
            logger.warn(CONF_NOT_FOUND.format(**locals()))
        else:
            out.append(conffn)
            luafn = "%s.lua" % utils.splitext_plus(conffn)[0]
            if os.path.exists(luafn):
                out.append(luafn)
    return out

def _default_conf_files(data):
    conf_files = []
    if dd.get_variantcaller(data):
        if annotate_gemini(data):
            conf_files.append("gemini")
        if _annotate_somatic(data):
            conf_files.append("somatic")
        if dd.get_analysis(data).lower().find("rna-seq") >= 0:
            conf_files.append("rnaedit")
    return conf_files

def annotate_gemini(data):
    """Annotate with population calls if have data installed.
    """
    r = dd.get_variation_resources(data)
    if r.get("exac") and os.path.exists(r["exac"]):
        return True
    return False

def _annotate_somatic(data):
    """Annotate somatic calls if we have cosmic data installed.
    """
    if is_human(data):
        paired = vcfutils.get_paired([data])
        if paired:
            r = dd.get_variation_resources(data)
            if r.get("cosmic") and os.path.exists(r["cosmic"]):
                return True
    return False

def is_human(data, builds=None):
    """Check if human, optionally with build number, search by name or extra GL contigs.
    """
    def has_build37_contigs(data):
        for contig in ref.file_contigs(dd.get_ref_file(data)):
            if contig.name.startswith("GL") or contig.name.find("_gl") >= 0:
                if contig.name in naming.GMAP["hg19"] or contig.name in naming.GMAP["GRCh37"]:
                    return True
        return False
    if not builds and tz.get_in(["genome_resources", "aliases", "human"], data):
        return True
    if not builds or "37" in builds:
        target_builds = ["hg19", "GRCh37"]
        if dd.get_genome_build(data) in target_builds:
            return True
        elif has_build37_contigs(data):
            return True
    if not builds or "38" in builds:
        target_builds = ["hg38"]
        if dd.get_genome_build(data) in target_builds:
            return True
    return False
