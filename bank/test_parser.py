"""Pure-Python tests (no Django needed):  python -m unittest bank.test_parser"""
import unittest
from pathlib import Path

from bank.parser import MCQ, TF, parse, to_text

GOOD = """BANK: Demo
Q: Which is correct?
NO: 5
A. one
B. two
C. three
ANSWER: b
EXPLANATION: because
  it wraps.
NOTE: source was messy

Q: Regarding RBCs:
TYPE: TF
A. Have a nucleus.
B. Contain carbonic anhydrase.
ANSWER: F, T
"""


class ParserTests(unittest.TestCase):
    def test_basic_mcq_and_tf(self):
        r = parse(GOOD)
        self.assertEqual(r.bank_title, "Demo")
        a, b = r.questions
        self.assertTrue(a.ok and b.ok, (a.errors, b.errors))
        self.assertEqual((a.kind, a.number, a.correct), (MCQ, "5", [False, True, False]))
        self.assertEqual(a.explanation, "because it wraps.")
        self.assertEqual(a.note, "source was messy")
        self.assertEqual((b.kind, b.correct), (TF, [False, True]))

    def test_multi_answer_accepts_lists(self):
        for ans in ("A, C", "A and C", "AC"[0] + "," + "C", "(a) (c)"):
            r = parse(f"Q: x\nA. 1\nB. 2\nC. 3\nANSWER: {ans}\n")
            self.assertEqual(r.questions[0].correct, [True, False, True], ans)

    def test_tf_answer_formats(self):
        for ans in ("TF", "T F", "true, false", "A=T, B=F", "a: true b: false"):
            r = parse(f"Q: x\nTYPE: TF\nA. p\nB. q\nANSWER: {ans}\n")
            self.assertEqual(r.questions[0].correct, [True, False], ans)

    def test_errors(self):
        cases = {
            "missing answer": "Q: x\nA. 1\nB. 2\n",
            "bad letter": "Q: x\nA. 1\nB. 2\nANSWER: D\n",
            "one option": "Q: x\nA. 1\nANSWER: A\n",
            "gap in letters": "Q: x\nA. 1\nC. 3\nANSWER: A\n",
            "tf wrong length": "Q: x\nTYPE: TF\nA. 1\nB. 2\nANSWER: T\n",
            "unknown type": "Q: x\nTYPE: essay\nA. 1\nB. 2\nANSWER: A\n",
            "empty option": "Q: x\nA. 1\nB.\nANSWER: A\n",
        }
        for name, txt in cases.items():
            self.assertFalse(parse(txt).questions[0].ok, name)

    def test_stray_and_unlabelled_lines(self):
        r = parse("Intro text\nQ: x\nA. 1\nB. 2\nANSWER: A\n\nrandom line\n")
        self.assertTrue(r.stray)
        self.assertFalse(r.questions[0].ok)  # unlabelled line after a blank is flagged

    def test_wrapped_stem_and_option(self):
        r = parse("Q: long stem\ncontinues here\nA. first\n  wrapped\nB. second\nANSWER: A\n")
        q = r.questions[0]
        self.assertEqual(q.stem, "long stem\ncontinues here")
        self.assertEqual(q.options[0], "first wrapped")
        self.assertTrue(q.ok)

    def test_duplicates_inside_batch(self):
        t = "Q: same\nA. 1\nB. 2\nANSWER: A\n\nQ: Same!\nA. 2\nB. 1\nANSWER: B\n"
        qs = parse(t).questions
        self.assertFalse(qs[0].duplicate_of)
        self.assertTrue(qs[1].duplicate_of)

    def test_seed_files_round_trip(self):
        files = sorted(Path(__file__).resolve().parent.parent.joinpath("data").glob("*.txt"))
        self.assertTrue(files)
        total = 0
        for f in files:
            txt = f.read_text(encoding="utf-8")
            r = parse(txt)
            total += len(r.questions)
            self.assertEqual([q for q in r.questions if q.errors], [], f.name)
            back = [dict(number=q.number, kind=q.kind, stem=q.stem,
                         options=list(zip(q.options, q.correct)),
                         explanation=q.explanation, note=q.note) for q in r.questions]
            self.assertEqual(to_text(r.bank_title, back), txt, f.name)
        self.assertEqual(total, 244)


if __name__ == "__main__":
    unittest.main()


class HeaderTests(unittest.TestCase):
    def test_subject_semester_deck_headers(self):
        r = parse("SUBJECT: Physiology\nSEMESTER: Semester 2\nDECK: Haematology\n\nQ: x\nA. 1\nB. 2\nANSWER: A\n")
        self.assertEqual((r.subject, r.semester, r.bank_title), ("Physiology", 2, "Haematology"))
        self.assertTrue(r.questions[0].ok)

    def test_bank_alias_and_export_headers(self):
        r = parse("BANK: Old style\nQ: x\nA. 1\nB. 2\nANSWER: A\n")
        self.assertEqual(r.bank_title, "Old style")
        out = to_text("D", [], subject="Anatomy", semester=3)
        self.assertTrue(out.startswith("SUBJECT: Anatomy\nSEMESTER: 3\nDECK: D"))
        r2 = parse(out)
        self.assertEqual((r2.subject, r2.semester, r2.bank_title), ("Anatomy", 3, "D"))


class TopicRemovedTests(unittest.TestCase):
    def test_stray_topic_line_is_ignored_not_merged_into_the_question(self):
        r = parse("Q: x\nTOPIC: Blood\nA. 1\nB. 2\nANSWER: A\n")
        q = r.questions[0]
        self.assertTrue(q.ok)
        self.assertEqual(q.stem, "x")
        self.assertTrue(any("TOPIC" in w for w in q.warnings))
        self.assertFalse(hasattr(q, "topic"))
