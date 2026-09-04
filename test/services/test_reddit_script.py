import unittest

from app.services import reddit_script


class TestNormalizeSelftext(unittest.TestCase):
    def test_strips_markdown_without_losing_the_words(self):
        text = (
            "My **husband** and I _agreed_ on this.\n\n"
            "See [this thread](https://reddit.com/x) or https://example.com/y.\n\n"
            "> She said she would help.\n\n"
            "# A heading\n"
            "~~struck out~~ and `code`."
        )
        result = reddit_script.normalize_selftext(text, expand=False)

        self.assertIn("My husband and I agreed on this.", result)
        self.assertIn("this thread", result)
        self.assertNotIn("https://", result)
        self.assertNotIn(">", result)
        self.assertNotIn("#", result)
        self.assertNotIn("~~", result)
        self.assertNotIn("`", result)

    def test_removes_double_escaped_zero_width_entities(self):
        # Reddit double-escapes some bodies; one unescape pass leaves the
        # entity sitting in the text as characters TTS would read aloud.
        result = reddit_script.normalize_selftext("&amp;#x200B;\n\nReal text here.")
        self.assertNotIn("200B", result)
        self.assertNotIn("&#", result)
        self.assertEqual(result, "Real text here.")

    def test_gives_unterminated_lines_a_full_stop(self):
        result = reddit_script.normalize_selftext(
            "- She smokes indoors\n- She rearranged the kitchen\n", expand=False
        )
        self.assertEqual(
            result, "She smokes indoors.\nShe rearranged the kitchen."
        )

    def test_drops_a_trailing_edit_block(self):
        text = (
            "This is the story and it goes on for a while so the edit "
            "lands past the sixty percent mark of the body text.\n\n"
            "More story to push the offset further along.\n\n"
            "EDIT: thanks for the awards, the verdict was NTA."
        )
        result = reddit_script.normalize_selftext(text, expand=False)
        self.assertNotIn("EDIT", result)
        self.assertNotIn("verdict", result)

    def test_keeps_an_update_that_opens_the_post(self):
        text = "UPDATE: she moved out.\n\n" + "Here is what happened next. " * 20
        result = reddit_script.normalize_selftext(text, expand=False)
        self.assertIn("she moved out", result)


class TestExpandJargon(unittest.TestCase):
    def test_expands_subreddit_shorthand(self):
        result = reddit_script.expand_jargon("AITA for this? My MIL says NTA.")
        self.assertIn("Am I the asshole", result)
        self.assertIn("mother-in-law", result)
        self.assertIn("Not the asshole", result)

    def test_leaves_ordinary_lowercase_words_alone(self):
        # "so", "op" and "info" are ordinary words; only the uppercase
        # acronym form should ever be rewritten.
        result = reddit_script.expand_jargon("so the op of the info was fine")
        self.assertEqual(result, "so the op of the info was fine")

    def test_longest_acronym_wins(self):
        result = reddit_script.expand_jargon("WIBTA if I left?")
        self.assertIn("Would I be the asshole", result)
        self.assertNotIn("Am I the asshole", result)

    def test_expands_age_and_gender_tokens(self):
        self.assertIn("28 female", reddit_script.expand_jargon("I (28F) said no"))
        self.assertIn("32 male", reddit_script.expand_jargon("my husband [32M]"))
        self.assertIn("30 female", reddit_script.expand_jargon("her (F30) friend"))


class TestSplitSentences(unittest.TestCase):
    def test_splits_on_terminators(self):
        self.assertEqual(
            reddit_script.split_sentences("One thing. Two things! Three?"),
            ["One thing.", "Two things!", "Three?"],
        )

    def test_keeps_abbreviations_intact(self):
        self.assertEqual(
            reddit_script.split_sentences("Dr. Smith said no. I agreed."),
            ["Dr. Smith said no.", "I agreed."],
        )


class TestSplitPost(unittest.TestCase):
    def _post(self, body: str, title: str = "AITA for saying no?") -> dict:
        return {
            "id": "abc123",
            "subreddit": "AmItheAsshole",
            "title": title,
            "selftext": body,
            "score": 900,
            "permalink": "https://www.reddit.com/r/AmItheAsshole/comments/abc123/",
        }

    def test_short_post_becomes_one_part_without_framing(self):
        split = reddit_script.split_post(
            self._post("A short story. It ends here."), part_seconds=60
        )
        self.assertEqual(len(split["parts"]), 1)
        part = split["parts"][0]
        self.assertEqual(part["total"], 1)
        self.assertNotIn("up next", part["script"])
        self.assertTrue(part["script"].startswith("Am I the asshole for saying no?"))

    def test_long_post_splits_into_parts_with_follow_on_lines(self):
        body = " ".join(f"This is sentence number {i}." for i in range(60))
        split = reddit_script.split_post(
            self._post(body), part_seconds=20, max_parts=10
        )

        self.assertGreater(len(split["parts"]), 1)
        for part in split["parts"][:-1]:
            self.assertIn("up next", part["script"])
        self.assertNotIn("up next", split["parts"][-1]["script"])
        self.assertTrue(split["parts"][1]["script"].startswith("Part 2."))

    def test_parts_respect_the_duration_budget_including_framing(self):
        # The framing is charged to the budget, so a part must not overshoot
        # the target once the title and follow-on line are counted.
        body = " ".join(f"Sentence number {i} of the story." for i in range(80))
        split = reddit_script.split_post(
            self._post(body), part_seconds=30, max_parts=20
        )
        for part in split["parts"]:
            self.assertLessEqual(part["estimated_seconds"], 33.0)

    def test_marks_truncated_when_the_story_exceeds_max_parts(self):
        body = " ".join(f"Sentence number {i} of the story." for i in range(200))
        split = reddit_script.split_post(
            self._post(body), part_seconds=20, max_parts=2
        )
        self.assertTrue(split["truncated"])
        self.assertEqual(len(split["parts"]), 2)

    def test_empty_body_yields_no_parts(self):
        split = reddit_script.split_post(self._post("   \n\n  "))
        self.assertEqual(split["parts"], [])

    def test_subject_carries_the_part_number(self):
        body = " ".join(f"This is sentence number {i}." for i in range(40))
        split = reddit_script.split_post(
            self._post(body), part_seconds=20, max_parts=10
        )
        self.assertIn("(Part 1/", split["parts"][0]["subject"])
        self.assertIn("r/AmItheAsshole", split["parts"][0]["subject"])


if __name__ == "__main__":
    unittest.main()
