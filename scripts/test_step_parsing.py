"""
test_step_parsing.py — Unit tests for step-parsing/splitting logic.

Run this BEFORE trusting any change to normalise.py or fix_broken_steps.py.
It exists because the eaten-marker bug (split_inline_numbered matching
"(6)" inside a running sentence and eating the marker, scrambling content
into fake steps) shipped to published data undetected — nobody tested the
splitting functions directly against adversarial input, only sampled real
data by hand after the fact. These tests encode the real bug cases found
so they can never silently regress again.

Usage:
    python3 scripts/test_step_parsing.py
    python3 scripts/test_step_parsing.py -v
"""

import importlib.util
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


normalise = _load("normalise")
fix_broken_steps = _load("fix_broken_steps")


class TestSplitInlineNumberedIsDisabled(unittest.TestCase):
    """
    Regression test for the exact bug found in production data:
    PMC13208427 ("Prediction Models for Frailty...") — a single sentence
    enumerating (1)...(6) got sliced into 6 scrambled fragments, eating
    each numeral marker. split_inline_numbered() must never do this again.
    """

    REAL_BUG_TEXT = (
        "Adherence to these internationally recognized standards will be "
        "maintained throughout all stages of the review process, "
        "encompassing: (1) the formulation of the research question and "
        "eligibility criteria; (2) systematic search strategy development "
        "and execution; (3) study selection; (4) data extraction; (5) risk "
        "of bias and applicability assessment using recommended tools "
        "(eg, PROBAST) [25]; and (6) narrative synthesis of findings. This "
        "structured approach will ensure methodological rigor."
    )

    def test_does_not_shred_inline_enumeration(self):
        result = fix_broken_steps.split_inline_numbered(self.REAL_BUG_TEXT)
        self.assertEqual(result, [], "split_inline_numbered must be disabled — "
                          "re-enabling it without a real paragraph-boundary guard "
                          "will re-shred inline enumeration like this")

    def test_formula_numbers_not_treated_as_steps(self):
        # star_protocols PMC10276142 — "(24)" inside a math expression got
        # treated as a step number, scrambling step order (1, 24, 2, 3, 23...).
        text = ("Compute f(r)=fmax×r+KD+n−(r+KD+n)2−4×r×n2×n,where KD is the "
                "apparent dissociation constant, as described in step (24).")
        result = fix_broken_steps.split_inline_numbered(text)
        self.assertEqual(result, [])


class TestStepsFromRaw(unittest.TestCase):
    """steps_from_raw() must preserve full paragraph content — it's the
    only path that turns raw source text into published step content."""

    def test_list_of_strings_preserved_whole(self):
        raw = [
            "Adherence to these internationally recognized standards will be "
            "maintained throughout all stages of the review process, "
            "encompassing: (1) the formulation of the research question and "
            "eligibility criteria; (2) systematic search strategy development "
            "and execution; and (6) narrative synthesis of findings.",
            "This is a second, independent paragraph from the source XML.",
        ]
        steps = normalise.steps_from_raw(raw)
        self.assertEqual(len(steps), 2, "each source paragraph should become exactly one step")
        self.assertIn("(1) the formulation", steps[0]["instruction"])
        self.assertIn("(6) narrative synthesis", steps[0]["instruction"],
                      "the marker must survive intact, not be eaten by a splitter")

    def test_string_input_does_not_eat_catalog_numbers(self):
        # zenodo 10818406 (Bathymophila williamsae sp. nov.) — the OLD
        # _split_text_into_steps() numbered-list regex matched "67177. "
        # (a museum catalog number followed by whitespace) as if it were a
        # step marker "67. ", and re.split() discarded it. "67177" was
        # completely gone from the published protocol — confirmed against
        # the untouched raw source. Affected 358 zenodo protocols.
        text = ("...depth 1295-1356 m; 14 May 2017; DNA tissue sample; "
                "MNHN-IM-2013-67177.   Paratype: 1 shell, same data as holotype.")
        steps = normalise.steps_from_raw(text)
        combined = " ".join(s["instruction"] for s in steps)
        self.assertIn("67177", combined, "catalog number must survive, not be eaten as a fake step marker")

    def test_list_of_dicts_preserved(self):
        raw = [
            {"title": "", "instruction": "Add 1 mL TRIzol directly to the well."},
            {"title": "", "instruction": "Incubate for 00:05:00 at room temperature."},
        ]
        steps = normalise.steps_from_raw(raw)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["instruction"], "Add 1 mL TRIzol directly to the well.")

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(normalise.steps_from_raw([]), [])
        self.assertEqual(normalise.steps_from_raw(None), [])
        self.assertEqual(normalise.steps_from_raw(""), [])

    def test_timer_detection_still_works(self):
        raw = [{"title": "", "instruction": "00:15:00"}]
        steps = normalise.steps_from_raw(raw)
        self.assertEqual(len(steps), 1)
        self.assertEqual(len(steps[0]["timers"]), 1)
        self.assertEqual(steps[0]["timers"][0]["duration_secs"], 900)


class TestClassifyHeader(unittest.TestCase):
    """fix_broken_steps.py's background/materials reclassification — the
    safe part that stayed enabled."""

    def test_materials_header_detected(self):
        self.assertEqual(fix_broken_steps.classify_header("Materials and Reagents"), "materials")
        self.assertEqual(fix_broken_steps.classify_header("Hardware"), "materials")
        self.assertEqual(fix_broken_steps.classify_header("Software"), "materials")

    def test_background_header_detected(self):
        self.assertEqual(fix_broken_steps.classify_header("Before You Begin"), "background")
        self.assertEqual(fix_broken_steps.classify_header("Abstract"), "background")

    def test_real_procedure_not_misclassified(self):
        self.assertEqual(
            fix_broken_steps.classify_header("Add 500 µL TRIzol to each well"),
            "procedure",
        )


class TestMidSentenceSplitDetector(unittest.TestCase):
    """The detector used by verify_protocol_quality.py to catch eaten-marker
    corruption — test the detector itself so it doesn't silently stop working."""

    def test_detects_known_bad_pattern(self):
        import verify_protocol_quality as vpq
        cur = "risk of bias and applicability assessment using recommended tools (eg, PROBAST) [25]; and ("
        nxt = "narrative synthesis of findings. This structured approach will ensure methodological rigor."
        self.assertTrue(vpq.looks_like_severed_sentence(cur, nxt))

    def test_does_not_flag_normal_step_boundary(self):
        import verify_protocol_quality as vpq
        cur = "Centrifuge at 12,000 x g for 15 minutes at 4 degrees C."
        nxt = "Discard the supernatant and resuspend the pellet in 500 uL TRIzol."
        self.assertFalse(vpq.looks_like_severed_sentence(cur, nxt))


if __name__ == "__main__":
    unittest.main(verbosity=2)
