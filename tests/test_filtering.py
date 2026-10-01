import unittest

import main


CITATIONS = [
    {"title": "Allowed", "url": "https://news.example.com/story"},
    {"title": "Blocked", "url": "https://blocked.test/story"},
]
SUMMARY = (
    "Allowed fact\u30101:0\u2020source\u3011. "
    "Blocked fact\u30102:0\u2020source\u3011. "
    "Uncited fact."
)


class DomainFilteringTests(unittest.TestCase):
    def test_repository_config_whitelists_microsoft(self):
        config = main.load_domain_filters()

        self.assertEqual(config["whitelist"], ["microsoft.com"])

    def test_matches_exact_domain_and_subdomains_only(self):
        self.assertTrue(main.domain_matches("example.com", "example.com"))
        self.assertTrue(main.domain_matches("news.example.com", "example.com"))
        self.assertFalse(main.domain_matches("notexample.com", "example.com"))

    def test_whitelist_removes_nonmatching_cited_sentence(self):
        result = main.filter_search_result(
            {
                "assistant_response": SUMMARY,
                "citations": CITATIONS,
                "raw_message": {"unfiltered": "secret"},
                "unknown_sdk_payload": {"unfiltered": "secret"},
            },
            "whitelist",
            {"whitelist": ["example.com"], "blacklist": []},
        )

        self.assertIn("Allowed fact", result["assistant_response"])
        self.assertNotIn("Blocked fact", result["assistant_response"])
        self.assertIn("Uncited fact", result["assistant_response"])
        self.assertEqual(result["citations"], [CITATIONS[0]])
        self.assertEqual(result["removed_sentence_count"], 1)
        self.assertNotIn("raw_message", result)
        self.assertNotIn("unknown_sdk_payload", result)

    def test_blacklist_removes_matching_cited_sentence(self):
        result = main.filter_search_result(
            {
                "assistant_response": SUMMARY,
                "citations": CITATIONS,
            },
            "blacklist",
            {"whitelist": [], "blacklist": ["blocked.test"]},
        )

        self.assertNotIn("Blocked fact", result["assistant_response"])
        self.assertEqual(result["citations"], [CITATIONS[0]])

    def test_empty_active_list_preserves_content_but_discards_raw_message(self):
        result = main.filter_search_result(
            {
                "assistant_response": SUMMARY,
                "citations": CITATIONS,
                "raw_message": {"unfiltered": "secret"},
            },
            "whitelist",
            {"whitelist": [], "blacklist": []},
        )

        self.assertEqual(result["assistant_response"], SUMMARY)
        self.assertEqual(result["citations"], CITATIONS)
        self.assertNotIn("raw_message", result)

    def test_mixed_source_sentence_is_removed_if_any_source_is_rejected(self):
        result = main.filter_search_result(
            {
                "assistant_response": (
                    "Mixed fact\u30101:0\u2020source\u3011"
                    "\u30102:0\u2020source\u3011. "
                    "Kept fact\u30101:0\u2020source\u3011."
                ),
                "citations": CITATIONS,
            },
            "whitelist",
            {"whitelist": ["example.com"], "blacklist": []},
        )

        self.assertNotIn("Mixed fact", result["assistant_response"])
        self.assertIn("Kept fact", result["assistant_response"])
        self.assertEqual(result["citations"], [CITATIONS[0]])

    def test_missing_markers_with_rejected_source_fails_closed(self):
        result = main.filter_search_result(
            {
                "assistant_response": "Content without citation markers.",
                "citations": CITATIONS,
            },
            "whitelist",
            {"whitelist": ["example.com"], "blacklist": []},
        )

        self.assertEqual(result["assistant_response"], "")
        self.assertEqual(result["citations"], [])
        self.assertEqual(result["removed_sentence_count"], 1)

    def test_rejects_invalid_citation_url_when_filtering_is_active(self):
        with self.assertRaisesRegex(ValueError, "Invalid citation URL"):
            main.filter_search_result(
                {
                    "assistant_response": "Bad source\u30101:0\u2020source\u3011.",
                    "citations": [{"title": "Bad", "url": "not a URL"}],
                },
                "whitelist",
                {"whitelist": ["example.com"], "blacklist": []},
            )

    def test_live_annotation_shape_retains_only_microsoft_reference(self):
        citations = [
            {"title": "Wikipedia", "url": "https://en.wikipedia.org/wiki/Microsoft"},
            {"title": "Tech Monitor", "url": "https://www.techmonitor.ai/microsoft"},
            {"title": "Britannica", "url": "https://www.britannica.com/microsoft"},
            {"title": "Microsoft", "url": "https://www.microsoft.com/en-us/about"},
            {"title": "Wikipedia duplicate", "url": "https://en.wikipedia.org/wiki/Microsoft"},
        ]
        result = main.filter_search_result(
            {
                "assistant_response": (
                    "External sources\u30103:0\u2020source\u3011"
                    "\u30103:4\u2020source\u3011. "
                    "Mixed sources\u30103:2\u2020source\u3011"
                    "\u30103:3\u2020source\u3011. "
                    "Microsoft only\u30103:3\u2020source\u3011."
                ),
                "citations": citations,
            },
            "whitelist",
            {"whitelist": ["microsoft.com"], "blacklist": []},
        )

        self.assertEqual(
            result["assistant_response"],
            "Microsoft only\u30103:3\u2020source\u3011.",
        )
        self.assertEqual(result["citations"], [citations[3]])
        self.assertEqual(result["configured_domain_count"], 1)


if __name__ == "__main__":
    unittest.main()
