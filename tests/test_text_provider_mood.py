from __future__ import annotations

import unittest
from pathlib import Path

from storyforge.providers.text import _infer_mood


class LocalMoodInferenceTests(unittest.TestCase):
    def test_recognizes_uncomplicated_romance(self) -> None:
        text = (
            "They fell in love after years apart. At their wedding, the bride "
            "kissed her soulmate, and both hearts finally felt at home."
        )

        self.assertEqual(_infer_mood(text), "romance")

    def test_recognizes_grief_without_treating_spouse_words_as_betrayal(self) -> None:
        text = (
            "His wife died before dawn. At the funeral, he cried through his "
            "grief and mourned the life they had lost."
        )

        self.assertEqual(_infer_mood(text), "sad")

    def test_recognizes_suspense(self) -> None:
        text = (
            "An unknown caller knew her secret. Then she found blood beside the "
            "missing woman's bag, and one clue pointed to murder."
        )

        self.assertEqual(_infer_mood(text), "suspense")

    def test_marital_betrayal_outweighs_incidental_romance_words(self) -> None:
        text = (
            "I had loved my husband since our beautiful wedding and trusted him "
            "with all my heart. Then a mistress sent proof that he had cheated on "
            "me throughout my pregnancy. He lied to his wife and promised the other "
            "woman a divorce. Betrayed and furious, I decided he would pay."
        )

        self.assertEqual(_infer_mood(text), "revenge")

    def test_b73165_style_betrayal_prefers_suspense_without_explicit_payback(self) -> None:
        text = (
            "The caller sent a hotel video of my husband in bed with his mistress. "
            "He had cheated while I was pregnant, then told her he would divorce his "
            "wife. I remembered our wedding, how much I loved him, and felt my heart "
            "break when I learned of the affair and betrayal."
        )

        self.assertEqual(_infer_mood(text), "suspense")

    def test_explicit_retaliation_turns_betrayal_into_revenge(self) -> None:
        text = (
            "The anonymous caller proved that my husband had cheated with his "
            "mistress. I quietly copied every bank record and decided to expose "
            "him in public. I would make him pay for the betrayal."
        )

        self.assertEqual(_infer_mood(text), "revenge")

    def test_strong_bereavement_remains_sad(self) -> None:
        text = (
            "After her daughter died, she sat through the funeral in stunned grief. "
            "She mourned every morning and cried until no tears remained."
        )

        self.assertEqual(_infer_mood(text), "sad")

    def test_full_b73165_manuscript_prefers_suspense(self) -> None:
        manuscript = Path(r"D:\work\book tools\input txt\B73165_GoodNovel.txt")
        if not manuscript.is_file():
            self.skipTest("User-provided B73165 manuscript is not available.")

        text = manuscript.read_text(encoding="utf-8-sig")

        self.assertGreater(len(text), 10_000)
        self.assertEqual(_infer_mood(text), "suspense")

    def test_relationship_nouns_alone_do_not_force_revenge(self) -> None:
        self.assertEqual(
            _infer_mood("A husband and wife drove home after dinner."),
            "suspense",
        )


if __name__ == "__main__":
    unittest.main()
