import tempfile
import unittest
from pathlib import Path

import main


class LongTermMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_memory_file = main._FACTS_FILE
        main._FACTS_FILE = Path(self.temp_dir.name) / "memory.json"

    def tearDown(self):
        main._FACTS_FILE = self.original_memory_file
        self.temp_dir.cleanup()

    def test_remember_stores_arbitrary_fact_and_deduplicates(self):
        first = main._remember_user_facts("remember that I prefer concise replies")
        second = main._remember_user_facts("remember that I prefer concise replies")
        self.assertEqual(first["content"], "I prefer concise replies")
        self.assertEqual(len(main._memory_items()), 1)
        self.assertEqual(first["id"], second["id"])

    def test_contact_relationship_remains_resolvable_and_is_remembered(self):
        main._remember_user_facts('remember "Hashir Qureshi" is my elder brother')
        self.assertEqual(main._contact_for_alias("my brother"), "Hashir Qureshi")
        self.assertEqual(main._contact_from_request("message my brother"), "Hashir Qureshi")
        self.assertEqual(len(main._memory_items()), 1)

    def test_forget_removes_fact_and_contact_aliases(self):
        main._remember_user_facts('remember "Hashir Qureshi" is my elder brother')
        removed = main._forget_memory("brother")
        self.assertTrue(removed)
        self.assertEqual(main._memory_items(), [])
        self.assertEqual(main._contact_for_alias("my brother"), "my brother")

    def test_forget_everything_clears_all_long_term_categories(self):
        main._remember_user_facts('remember "Azka CA Lhr" is my buddy')
        removed = main._forget_memory("everything you remember about me")
        self.assertTrue(removed)
        self.assertEqual(main._memory_items(), [])
        self.assertEqual(main._load_facts().get("contacts"), {})

    def test_relevant_memory_ranking_and_secret_rejection(self):
        main._remember_user_facts("remember that my preferred language is Urdu")
        main._remember_user_facts("remember that I prefer concise replies")
        relevant = main._relevant_memory_items("reply in Urdu")
        self.assertIn("Urdu", relevant[0]["content"])
        with self.assertRaises(ValueError):
            main._remember_user_facts("remember my API key is abc123")


if __name__ == "__main__":
    unittest.main()
