import unittest

from . import converter


SOURCE = """**1. A patient is given a small dose of radioactive sodium (Na+). Most of this sodium will distribute in:

A. Plasma

B. Interstitial fluid

C. Intracellular fluid

D. Extracellular fluid

E. Transcellular fluid

Correct Answer: D — Extracellular fluid

Explanation: Sodium is the major extracellular cation.

211. Mention 4 functions of calcium.
Correct Answer: Bone and tooth mineralization; blood coagulation; neuromuscular excitation and contraction; and intracellular signaling.
Explanation: Calcium participates in mineralization and cell signaling.
"""


class ConverterTests(unittest.TestCase):
    def test_converts_mcq_and_descriptive_and_validates_output(self):
        result = converter.convert_source(
            SOURCE, subject="Physiology", semester="3", deck_title="Body Fluids"
        )
        self.assertEqual(len(result.questions), 2)
        self.assertTrue(all(question.ok for question in result.questions))
        self.assertIn("SUBJECT: Physiology", result.output)
        self.assertIn("SEMESTER: 3", result.output)
        self.assertIn("DECK: Body Fluids", result.output)
        self.assertIn("ANSWER: D", result.output)
        self.assertIn("TYPE: DESC", result.output)
        self.assertIn("NO: 211", result.output)
        self.assertEqual(result.questions[0].correct, [False, False, False, True, False])
        self.assertEqual(result.questions[1].answer_raw.startswith("Bone and tooth"), True)

    def test_manual_review_is_preserved_and_import_parser_rejects_it(self):
        source = """2. Generalized edema caused by:
A. Failure of left ventricle
B. Central venous pressure
C. Pulmonary artery stenosis
Correct Answer: Manual Review Required
Explanation: The stem is incomplete.
"""
        result = converter.convert_source(source)
        question = result.questions[0]
        self.assertFalse(question.ok)
        self.assertTrue(any("ANSWER is missing" in error for error in question.errors))
        self.assertIn("Manual review required in source", question.note)

    def test_converts_full_mixed_example_without_guessing_manual_review(self):
        source = """**1. A patient is given a small dose of radioactive sodium (Na+). Most of this sodium will distribute in:
A. Plasma
B. Interstitial fluid
C. Intracellular fluid
D. Extracellular fluid
E. Transcellular fluid
Correct Answer: D — Extracellular fluid
Explanation: Sodium is the major extracellular cation.

2. Generalized edema caused by:
A. Failure of left ventricle
B. Central venous pressure
C. Pulmonary artery stenosis
Correct Answer: Manual Review Required
Explanation: The stem is incomplete and the answer is unclear.

3. What combinations measure intracellular fluid volume?
A. Radioactive sodium & heavy water
B. Tritium oxide & Evan’s blue dye
C. Sucrose & Evan’s blue dye
D. Deuterium oxide & iodine labeled albumin
Correct Answer: A — Radioactive sodium & heavy water
Explanation: ICF is derived from total body water minus ECF.

4. The First Line of Defense after infusion of fixed acid:
A. ICF Buffer.
B. ECF Buffer.
C. Increase H+ Secretion.
D. Increase ammonia production.
E. Increase Respiratory Rate.
Correct Answer: B — ECF Buffer.
Explanation: Chemical buffers respond first.

5. The following ion is higher in ICF than ECF:
A. Sodium.
B. Potassium.
C. Calcium.
D. Bicarbonate.
E. Chloride.
Correct Answer: B — Potassium.
Explanation: Potassium is the principal intracellular cation.
"""
        result = converter.convert_source(source)
        self.assertEqual(len(result.questions), 5)
        self.assertEqual(
            [question.ok for question in result.questions],
            [True, False, True, True, True],
        )
        self.assertEqual(result.questions[0].correct, [False, False, False, True, False])
        self.assertEqual(result.questions[2].correct, [True, False, False, False])
        self.assertEqual(result.questions[3].correct, [False, True, False, False, False])
        self.assertEqual(result.questions[4].correct, [False, True, False, False, False])
        self.assertIn("NO: 5", result.output)
        self.assertIn("Manual Review Required", result.output)

    def test_answer_label_mismatch_is_not_guessed(self):
        source = """1. Pick the correct item:
A. Alpha
B. Beta
Correct Answer: B — Gamma
"""
        question = converter.convert_source(source).questions[0]
        self.assertFalse(question.ok)
        self.assertFalse(any(question.correct))
        self.assertTrue(any("ANSWER is missing" in error for error in question.errors))
        self.assertIn("does not match", question.note)

    def test_misordered_source_options_are_flagged_not_renumbered_silently(self):
        source = """1. Choose an option:
B. Beta
C. Gamma
Correct Answer: B — Beta
"""
        question = converter.convert_source(source).questions[0]
        self.assertFalse(question.ok)
        self.assertFalse(any(question.correct))
        self.assertIn("Source options must be labeled", question.note)

    def test_invalid_semester_and_preamble_are_reported(self):
        result = converter.convert_source("Intro\n1. Describe calcium.\nCorrect Answer: Helps signaling.\n",
                                          semester="13")
        self.assertEqual(len(result.questions), 1)
        self.assertTrue(result.questions[0].ok)
        self.assertTrue(any("before the first numbered question" in issue for issue in result.issues))
        self.assertTrue(any("outside the supported range" in issue for issue in result.issues))
        self.assertNotIn("SEMESTER:", result.output)


if __name__ == "__main__":
    unittest.main()
