"""Run: python3 -m unittest discover -s benchmarks/tests -v"""
import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('prepare', Path(__file__).resolve().parents[1] / 'prepare.py')
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)

TITLE = 'Aspirin reduces colon cancer risk.'
ABSTRACT = 'In mice, aspirin lowered PTGS2 expression and colon cancer incidence.'
PUBTATOR = '\n'.join([
    f'111|t|{TITLE}',
    f'111|a|{ABSTRACT}',
    '111\t0\t7\tAspirin\tChemicalEntity\tD001241',
    '111\t16\t28\tcolon cancer\tDiseaseOrPhenotypicFeature\tD003110',
    f'111\t{len(TITLE) + 1 + 3}\t{len(TITLE) + 1 + 7}\tmice\tOrganismTaxon\t10090',
    f'111\t{len(TITLE) + 1 + 9}\t{len(TITLE) + 1 + 16}\taspirin\tChemicalEntity\tD001241',
    f'111\t{len(TITLE) + 1 + 25}\t{len(TITLE) + 1 + 30}\tPTGS2\tGeneOrGeneProduct\t5743,19225',
    '111\tNegative_Correlation\tD001241\tD003110\tNovel',
    '111\tNegative_Correlation\tD001241\t5743\tNo',
    '',
])

DIMBRE = json.dumps({
    'doc_key': '999',
    'sentences': [['Inulin', 'boosts', 'Bifidobacterium', '.'], ['It', 'may', 'lower', 'plasma', 'glucose', '.']],
    'ner': [[[0, 0, 'Nutrient'], [2, 2, 'Microorganism']], [[7, 8, 'Chemical']]],
    'triggers': [[[1, 1, 'INCREASES']], [[6, 6, 'DECREASES']]],
    'relations': [[[0, 0, 2, 2, 'INCREASES', 'Factual']], [[0, 0, 7, 8, 'DECREASES', 'Unknown']]],
    'triplets': [[], []],
})


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.raw, self.out = self.root / 'raw', self.root / 'out'

    def tearDown(self):
        self.tmp.cleanup()

    def write_biored_zip(self, text):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as z:
            z.writestr('BioRED/Test.PubTator', text)
        (self.raw / 'biored').mkdir(parents=True)
        (self.raw / 'biored' / 'BIORED.zip').write_bytes(buffer.getvalue())

    def test_biored_offsets_hold_on_concatenated_text(self):
        self.write_biored_zip(PUBTATOR)
        stats = prepare.prepare('biored', 'test', self.raw, self.out)
        self.assertEqual(stats['mention_offsets_checked'], 5)
        docs = json.loads((self.out / 'documents.biored-test.json').read_text())
        gold = json.loads((self.out / 'gold.biored-test.json').read_text())
        text = docs[0]['blocks'][0]['text']
        self.assertEqual(text, TITLE + ' ' + ABSTRACT)
        self.assertEqual(docs[0]['id'], 'biored:111')
        self.assertEqual(docs[0]['external_id'], 'biored:111')
        for entity in gold['entities']:
            self.assertEqual(text[entity['start']:entity['end']], entity['text'])
        self.assertEqual(gold['entities'][-1]['ids'], ['5743', '19225'])
        self.assertEqual(gold['labels']['relation_types'], ['Negative_Correlation'])
        self.assertEqual(gold['labels']['novelty'], ['No', 'Novel'])
        manifest = json.loads((self.out / 'manifest.json').read_text())
        self.assertEqual(len(manifest['outputs']['biored-test']['source']['files'][0]['sha256']), 64)

    def test_biored_offset_mismatch_fails(self):
        self.write_biored_zip(PUBTATOR.replace('\t0\t7\tAspirin', '\t1\t8\tAspirin'))
        with self.assertRaises(prepare.PrepareError):
            prepare.prepare('biored', 'test', self.raw, self.out)

    def test_file_without_relations_is_rejected(self):
        self.write_biored_zip('\n'.join(l for l in PUBTATOR.splitlines() if 'Correlation' not in l))
        with self.assertRaisesRegex(prepare.PrepareError, 'no relation'):
            prepare.prepare('biored', 'test', self.raw, self.out)

    def test_dimbre_spans_reproduce_tokens_and_sentences(self):
        target = self.raw / 'dimbre' / prepare.DIMBRE_COMMIT / 'test.json'
        target.parent.mkdir(parents=True)
        target.write_text(DIMBRE + '\n')
        stats = prepare.prepare('dimbre', 'test', self.raw, self.out)
        self.assertEqual(stats['cross_sentence_relations'], 1)
        docs = json.loads((self.out / 'documents.dimbre-test.json').read_text())
        gold = json.loads((self.out / 'gold.dimbre-test.json').read_text())
        text = docs[0]['blocks'][0]['text']
        self.assertEqual(text, 'Inulin boosts Bifidobacterium . It may lower plasma glucose .')
        self.assertEqual([text[s['start']:s['end']] for s in gold['sentences']],
                         ['Inulin boosts Bifidobacterium .', 'It may lower plasma glucose .'])
        chem = gold['entities'][-1]
        self.assertEqual((chem['text'], text[chem['start']:chem['end']], chem['sentence_index']), ('plasma glucose', 'plasma glucose', 1))
        rel = gold['relations'][1]
        self.assertEqual((rel['head_text'], rel['tail_text'], rel['factuality'], rel['sentence_indices']),
                         ('Inulin', 'plasma glucose', 'Unknown', [0, 1]))
        self.assertEqual(gold['labels']['factuality'], ['Factual', 'Unknown'])
        self.assertIn('layout differs', docs[0]['text_reconstruction'])


if __name__ == '__main__':
    unittest.main()
