import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config, ConfigError, _mini_yaml, deep_merge, load  # noqa: E402

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"


class TestMiniYaml(unittest.TestCase):
    def test_matches_pyyaml_on_the_example_config(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        text = EXAMPLE.read_text(encoding="utf-8")
        self.assertEqual(yaml.safe_load(text), _mini_yaml(text))

    def test_scalars_and_lists(self):
        parsed = _mini_yaml(
            "a: 1\nb: true\nc: \"quoted: colon\"\nd:\n  - x\n  - y\ne:\n  f: 0.5\n"
        )
        self.assertEqual(parsed["a"], 1)
        self.assertIs(parsed["b"], True)
        self.assertEqual(parsed["c"], "quoted: colon")
        self.assertEqual(parsed["d"], ["x", "y"])
        self.assertEqual(parsed["e"]["f"], 0.5)

    def test_unindented_list(self):
        self.assertEqual(_mini_yaml("roots:\n- /Photos\nx: 1\n"),
                         {"roots": ["/Photos"], "x": 1})

    def test_comments_ignored_but_hash_in_value_kept(self):
        parsed = _mini_yaml('# lead\nkey: value # trailing\nurl: "http://h/#frag"\n')
        self.assertEqual(parsed["key"], "value")
        self.assertEqual(parsed["url"], "http://h/#frag")

    def test_roots_with_spaces_and_hyphens(self):
        """Real Proton folder names: "Photos from 2024", "Albums 2019 - 2026 google"."""
        parsed = _mini_yaml(
            "proton:\n"
            "  roots:\n"
            "    - /my-files/Photos/Photos from 2024\n"
            '    - "/my-files/Photos/Albums 2019 - 2026 google"\n'
            "  max_depth: 25\n"
        )
        self.assertEqual(parsed["proton"]["roots"], [
            "/my-files/Photos/Photos from 2024",
            "/my-files/Photos/Albums 2019 - 2026 google",
        ])
        self.assertEqual(parsed["proton"]["max_depth"], 25)

    def test_a_year_folder_name_stays_a_string(self):
        # "Photos from 2024" must not be coerced by the number sniffing.
        self.assertEqual(_mini_yaml("roots:\n  - Photos from 2024\n")["roots"],
                         ["Photos from 2024"])

    def test_multiple_roots_override_the_default(self):
        cfg = Config({"proton": {"roots": ["/a", "/b"]}})
        self.assertEqual(cfg.get("proton.roots"), ["/a", "/b"])

    def test_maps_in_lists_rejected_clearly(self):
        with self.assertRaises(ConfigError):
            _mini_yaml("items:\n  - name: a\n")


class TestConfig(unittest.TestCase):
    def test_defaults_and_paths(self):
        cfg = load(None)
        self.assertTrue(str(cfg.db_path).endswith(".state/state.sqlite"))
        self.assertEqual(cfg.ready_dir.name, "ready")

    def test_deep_merge_keeps_untouched_defaults(self):
        merged = deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"y": 9}})
        self.assertEqual(merged, {"a": {"x": 1, "y": 9}})

    def test_validate_requires_api_suffix_and_key(self):
        cfg = Config({"immich": {"url": "http://vm:2283", "api_key": "k"},
                      "proton": {"roots": ["/Photos"], "backend": "proton-cli"}})
        cfg.set("immich.upload_mode", "cli")
        self.assertTrue(any("/api" in p for p in cfg.validate()))
        cfg.set("immich.url", "http://vm:2283/api")
        self.assertEqual(cfg.validate(), [])

    def test_env_override(self):
        import os
        os.environ["IMMICH_API_KEY"] = "from-env"
        try:
            self.assertEqual(load(None).get("immich.api_key"), "from-env")
        finally:
            del os.environ["IMMICH_API_KEY"]


if __name__ == "__main__":
    unittest.main()
