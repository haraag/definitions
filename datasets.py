# -*- coding: UTF-8 -*-
from __future__ import unicode_literals, print_function
import os
import re
import zipfile
from pathlib2 import Path
from rdflib import Graph, ConjunctiveGraph
from lxltools.datacompiler import (Compiler, load_json, read_csv, decorate,
        construct, to_jsonld, to_rdf)
from lxltools.contextmaker import DEFAULT_NS_PREF_ORDER, make_context, add_overlay


# TODO:
# - explicitly link each record to it's parent dataset record (c.f. ldp:IndirectContainer)
# - explicitly link each record to its logical source (or just the parent dataset record?)
# - do not add 'quoted' here but in loader (see TODO below)


BASE = "https://id.kb.se/"

SCRIPT_DIR = Path(__file__).parent

scriptpath = lambda pth: str(SCRIPT_DIR / pth)


def build_jsonld(graph):
    path = "sys/context/base.jsonld"
    return to_jsonld(graph, ("../"+path, scriptpath(path)))

def to_camel_case(label):
    return "".join((s[0].upper() if i else s[0].lower()) + s[1:]
            for (i, s) in enumerate(re.split(r'[\s,.-]', label)) if s)

def _get_zipped_graph(path, name):
    with zipfile.ZipFile(path, 'r') as zipped:
        return Graph().parse(zipped.open(name), format='turtle')

def _get_repo_version():
    try:
        with os.popen(
                '(cd {} && git describe --tags)'.format(SCRIPT_DIR)
                ) as pipe:
            return pipe.read().rstrip()
    except:
        return None

class WhelkCompiler(Compiler):

    def to_node_description(self, node, dataset=None, source=None, **kws):
        # TODO: overhaul these? E.g. mainEntity with timestamp and 'datasource'.
        item = node.pop('mainEntity', None) # TODO: obsolete?
        if item:
            node['mainEntity'] = {'@id': item['@id']}
        #if dataset:
        #    node['inDataset'] = {'@id': dataset}
        #if source:
        #    node['wasDerivedFrom'] = {'@id': source}

        items = [node]
        if item:
            items.append(item)

        return {'@graph': items}


compiler = WhelkCompiler(dataset_id=BASE + 'definitions', union='definitions.jsonld.lines')


#@compiler.dataset
#def datasets():
#    graph = Graph().parse(scriptpath('source/index.ttl'), format='turtle')
#    return to_jsonld(graph, (None, None), { "@language": "sv"})


# NOTE: this step is currently part of the source maintenance, used to sync
# with "unstable" marcframe mappings. I plan to inverse parts of this flow
# to generate token-maps (used by marcframe processors) from these vocab
# and enum sources instead.
#prep_vocab_data():
#    python scripts/vocab-from-marcframe.py
#           ext-libris/src/main/resources/marcframe.json build/vocab.ttl
#           > build/vocab-generated-source-1.ttl

@compiler.handler
def vocab():
    vocab_base = BASE + 'vocab/'

    graph = construct(compiler.cached_rdf, sources=[
            {
                'source': Graph().parse(scriptpath('source/vocab/bf-map.ttl'), format='turtle'),
                'dataset': '?'
            },
            {"source": "http://id.loc.gov/ontologies/bibframe/"}
        ],
        query=scriptpath("source/vocab/bf-to-kbv-base.rq"))

    for part in (SCRIPT_DIR/'source/vocab').glob('**/*.ttl'):
        graph.parse(str(part), format='turtle')

    #cg = ConjunctiveGraph()
    #cg.parse(str(SCRIPT_DIR/'source/vocab/display.jsonld'), format='json-ld')
    #graph += cg

    graph.update((SCRIPT_DIR/'source/vocab/update.rq').read_text())

    with open(scriptpath('source/vocab/construct-enum-restrictions.rq')) as f:
        graph += Graph().query(f.read()).graph

    for part in (SCRIPT_DIR/'source/marc').glob('**/*.ttl'):
        graph.parse(str(part), format='turtle')

    data = build_jsonld(graph)
    del data['@context']

    # Put /vocab/* first (ensures that /marc/* comes after)
    data['@graph'] = sorted(data['@graph'], key=lambda node:
            (not node.get('@id', '').startswith(vocab_base), node.get('@id')))

    version = _get_repo_version()
    if version:
        data['@graph'][0]['version'] = version

    lib_context = make_context(graph, vocab_base, DEFAULT_NS_PREF_ORDER)
    add_overlay(lib_context, load_json(scriptpath('sys/context/base.jsonld')))
    add_overlay(lib_context, load_json(scriptpath('source/vocab-overlay.jsonld')))
    lib_context['@graph'] = [{'@id': BASE + 'vocab/context'}]

    compiler.write(lib_context, 'vocab/context')
    compiler.write(load_json(str(SCRIPT_DIR/'source/vocab/display.jsonld')), 'vocab/display')
    compiler.write(data, "vocab")


@compiler.dataset
def enums():
    graph = Graph()
    # These definitions are now included in the vocab package
    #graph.parse(scriptpath('source/marc/enums.ttl'), format='turtle')

    with open(scriptpath('source/marc/construct-enums.rq')) as f:
        graph += Graph().query(f.read()).graph

    return "/marc/", build_jsonld(graph)


@compiler.dataset
def schemes():
    graph = Graph().parse(scriptpath('source/schemes.ttl'), format='turtle')
    return "/term/", build_jsonld(graph)


@compiler.dataset
def relators():
    def relitem(item):
        mk_uri = lambda leaf: BASE + "relator/" + leaf
        item['@id'] = mk_uri(item.get('term') or
                to_camel_case(item['label_en'].strip()))
        item['sameAs'] = {'@id': mk_uri(item['code'])}
        return item
    graph = construct(compiler.cached_rdf, sources=[
            {
                "source": map(relitem, read_csv(scriptpath('source/funktionskoder.tsv'))),
                "dataset": BASE + "dataset/relators",
                "context": [scriptpath("sys/context/ns.jsonld"), {
                    "code": "skos:notation",
                    "label_sv": {"@id": "skos:prefLabel", "@language": "sv"},
                    "label_en": {"@id": "skos:prefLabel", "@language": "en"},
                    "comment_sv": {"@id": "rdfs:comment", "@language": "sv"},
                    "term": "rdfs:label",
                    "sameAs": "owl:sameAs"
                }]
            },
            {
                "source": "http://id.loc.gov/vocabulary/relators"
            }
        ],
        query=scriptpath("source/construct-relators.rq"))
    return "/relator/", build_jsonld(graph)


@compiler.dataset
def languages():
    loclangpath, fmt = compiler.get_cached_path("loc-language-data.ttl"), 'turtle'
    loclanggraph = Graph()
    if not Path(loclangpath).exists():
        # More than <http://id.loc.gov/vocabulary/iso639-*> but without inferred SKOS
        cherry_pick_loc_lang_data = (
                SCRIPT_DIR/'source/construct-loc-language-data.rq').read_text()
        loclanggraph += _get_zipped_graph(
                compiler.cache_url('http://id.loc.gov/static/data/vocabularyiso639-1.ttl.zip'),
                'iso6391.ttl').query(cherry_pick_loc_lang_data)
        loclanggraph += _get_zipped_graph(
                compiler.cache_url('http://id.loc.gov/static/data/vocabularyiso639-2.ttl.zip'),
                'iso6392.ttl').query(cherry_pick_loc_lang_data)
        loclanggraph.serialize(loclangpath, format=fmt)
    else:
        loclanggraph.parse(loclangpath, format=fmt)

    languages = decorate(read_csv(scriptpath('source/spraakkoder.tsv')),
        {"@id": BASE + "language/{code}"})

    graph = construct(compiler.cached_rdf, sources=[
            {
                "source": languages,
                "dataset": BASE + "dataset/languages",
                "context": load_json(scriptpath("source/table-context.jsonld"))['@context']
            },
            {
                "source": loclanggraph,
                "dataset": "http://id.loc.gov/vocabulary/languages"
            }
        ],
        query=scriptpath("source/construct-languages.rq"))

    return "/language/", build_jsonld(graph)


@compiler.dataset
def countries():
    graph = construct(compiler.cached_rdf, sources=[
            {
                "source": decorate(read_csv(scriptpath('source/landskoder.tsv')),
                    {"@id": BASE + "country/{code}"}),
                "dataset": BASE + "dataset/countries",
                # TODO: fix rdflib_jsonld so urls in external contexts are loaded
                "context": load_json(scriptpath("source/table-context.jsonld"))['@context']
            },
            {
                "source": "http://id.loc.gov/vocabulary/countries"
            }
        ],
        query=scriptpath("source/construct-countries.rq"))
    return "/country/", build_jsonld(graph)


@compiler.dataset
def nationalities():
    graph = Graph()
    items = decorate(read_csv(scriptpath('source/nationalitetskoder.tsv'), encoding='latin-1'),
            {"@id": BASE + "nationality/{code}", "@type": 'Nationality'})
    to_rdf(items, graph, context_data=[scriptpath("sys/context/base.jsonld"), {
            "label_sv": {"@id": "rdfs:label", "@language": "sv"
        }}])
    return "/nationality/", build_jsonld(graph)


@compiler.dataset
def docs():
    import markdown
    docs = []
    for fpath in (SCRIPT_DIR/'source/doc').glob('**/*.mkd'):
        text = fpath.read_text(encoding='utf-8')
        html = markdown.markdown(text)
        doc_id = (str(fpath)
                  .replace(os.sep, '/')
                  .replace('source/', '')
                  .replace('.mkd', ''))
        doc_id, dot, lang = doc_id.partition('.')
        doc = {
            "@type": "Article",
            "@id": BASE + doc_id,
            "articleBody": html
        }
        h1end = html.find('</h1>')
        if h1end > -1:
            doc['title'] = html[len('<h1>'):h1end]
        if lang:
            doc['language'] = {"langTag": lang},
        docs.append(doc)

    return "/doc", {
        "@context": "../sys/context/base.jsonld",
        "@graph": docs
    }


if __name__ == '__main__':
    import argparse

    argp = argparse.ArgumentParser(
            description="Available datasets: " + ", ".join(compiler.datasets),
            formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    arg = argp.add_argument
    arg('-o', '--outdir', type=str, default=scriptpath("build"), help="Output directory")
    arg('-c', '--cache', type=str, default=scriptpath("cache"), help="Cache directory")
    arg('-l', '--lines', action='store_true',
            help="Output a single file with one JSON-LD document per line")
    arg('datasets', metavar='DATASET', nargs='*')

    args = argp.parse_args()
    if not args.datasets and args.outdir:
        args.datasets = list(compiler.datasets)

    compiler.configure(args.outdir, args.cache, use_union=args.lines)
    compiler.run(args.datasets)
