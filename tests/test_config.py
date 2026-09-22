import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import (DEFAULTS, Config, ConfigError, _mini_yaml,  # noqa: E402
                        deep_merge, load)

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"
ACCOUNTS_EXAMPLE = (Path(__file__).resolve().parent.parent
                    / "config.accounts.example.yaml")


class TestMiniYaml(unittest.TestCase):
    def test_matches_pyyaml_on_the_example_config(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        text = EXAMPLE.read_text(encoding="utf-8")
        self.assertEqual(yaml.safe_load(text), _mini_yaml(text))

    def test_the_builtin_parser_refuses_the_accounts_example_clearly(self):
        """It cannot read maps inside a list, and the error must say so
        rather than silently producing a config with no accounts."""
        text = ACCOUNTS_EXAMPLE.read_text(encoding="utf-8")
        with self.assertRaises(ConfigError) as ctx:
            _mini_yaml(text)
        self.assertIn("PyYAML", str(ctx.exception))

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
        self.assertTrue(str(cfg.db_path).endswith(".state/default.sqlite"),
                        "one database per pipeline, named for the account")
        self.assertTrue(str(cfg.legacy_db_path).endswith(".state/state.sqlite"),
                        "the pre-split path, which only the migration reads")
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


class TestAccountToken(unittest.TestCase):
    """`{account}` in the shared config, so a fifth person is one line."""

    def build(self, names, **shared):
        data = copy.deepcopy(DEFAULTS)
        data["immich"]["url"] = "http://vm:2283/api"
        data["immich"]["api_key_file"] = "/secrets/{account}.key"
        data["staging"]["root"] = "/staging/{account}"
        for dotted, value in shared.items():
            node = data
            parts = dotted.replace("__", ".").split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        data["accounts"] = [{"name": n} for n in names]
        return Config(data)

    def test_five_accounts_from_five_one_line_entries(self):
        cfg = self.build(["default", "mirjam", "alice", "bob", "kid"])
        self.assertEqual(cfg.validate(), [])
        for account in cfg.accounts:
            name = account.account_name
            self.assertEqual(str(account.staging), f"/staging/{name}")
            self.assertEqual(account.get("immich.api_key_file"),
                             f"/secrets/{name}.key")
            self.assertEqual(account.db_path.name, f"{name}.sqlite")

    def test_the_three_things_that_must_differ_cannot_collide(self):
        """Derived from the name, so copy-paste cannot make two people share
        a staging directory or a key."""
        cfg = self.build(["a", "b", "c", "d", "e"])
        self.assertEqual(len({str(x.staging) for x in cfg.accounts}), 5)
        self.assertEqual(len({str(x.proton_cache_dir) for x in cfg.accounts}), 5)
        self.assertEqual(
            len({x.get("immich.api_key_file") for x in cfg.accounts}), 5)

    def test_the_token_works_in_the_single_account_shape_too(self):
        data = copy.deepcopy(DEFAULTS)
        data["immich"]["url"] = "http://vm:2283/api"
        data["immich"]["api_key_file"] = "/secrets/{account}.key"
        data["staging"]["root"] = "/staging/{account}"
        data["account"] = {"name": "solo"}
        account = Config(data).account(None)
        self.assertEqual(str(account.staging), "/staging/solo")
        self.assertEqual(account.get("immich.api_key_file"), "/secrets/solo.key")

    def test_the_state_directory_is_never_templated(self):
        """It is shared on purpose: the UI reads every database from one
        place. A `{account}` there would split them."""
        cfg = self.build(["a", "b"], state__dir="/state/{account}")
        self.assertEqual(len({str(x.state_dir) for x in cfg.accounts}), 1)

    def test_cmd_templates_are_left_alone(self):
        """`proton.cmd` carries {path} and {dest_dir}; a str.format() pass
        over the config would raise on them."""
        cfg = self.build(["a"])
        self.assertIn("{path}", cfg.accounts[0].get("proton.cmd.download"))
        self.assertIn("{dest_dir}", cfg.accounts[0].get("proton.cmd.download"))

    def test_the_token_expands_inside_lists_and_nested_maps(self):
        cfg = self.build(["alice"], proton__roots=["/my-files/{account}",
                                                   "/other/{account}/raw"])
        self.assertEqual(cfg.accounts[0].get("proton.roots"),
                         ["/my-files/alice", "/other/alice/raw"])

    def test_an_account_entry_can_still_override_anything(self):
        data = copy.deepcopy(DEFAULTS)
        data["immich"]["url"] = "http://vm:2283/api"
        data["immich"]["api_key_file"] = "/secrets/{account}.key"
        data["staging"]["root"] = "/staging/{account}"
        data["accounts"] = [
            {"name": "alice"},
            {"name": "bob", "immich_url": "http://other:2283/api",
             "staging_dir": "/elsewhere/bob"},
        ]
        cfg = Config(data)
        self.assertEqual(cfg.validate(), [])
        self.assertEqual(cfg.account("bob").get("immich.url"),
                         "http://other:2283/api")
        self.assertEqual(str(cfg.account("bob").staging), "/elsewhere/bob")
        self.assertEqual(cfg.account("alice").get("immich.url"),
                         "http://vm:2283/api")


class TestAccountsExample(unittest.TestCase):
    def test_the_shipped_two_account_example_is_valid(self):
        try:
            import yaml  # noqa: F401  -- the import *is* the check
        except ImportError:
            self.skipTest("PyYAML not installed")
        cfg = load(ACCOUNTS_EXAMPLE)
        self.assertEqual(cfg.validate(), [])
        names = [a.account_name for a in cfg.accounts]
        self.assertEqual(names, ["default", "mirjam"])

        # The first account is `default` on purpose: it is what an existing
        # single-account install already has stamped on every row, so keeping
        # it means no database rename. And `default` is special-cased, so its
        # lock, status.json and MQTT identity keep their v1 names and Home
        # Assistant entities do not move.
        first = cfg.account("default")
        self.assertEqual(first.lock_path.name, "sync.lock")
        self.assertEqual(first.status_path.name, "status.json")
        second = cfg.account("mirjam")
        self.assertEqual(second.lock_path.name, "sync-mirjam.lock")
        self.assertEqual(second.status_path.name, "status-mirjam.json")
        # The three things that must never collide.
        self.assertEqual(len({str(a.staging) for a in cfg.accounts}), 2)
        self.assertEqual(len({str(a.proton_cache_dir) for a in cfg.accounts}), 2)
        self.assertEqual(
            len({a.get("immich.api_key_file") for a in cfg.accounts}), 2)
        # ...and one database each, in one shared directory.
        self.assertEqual(len({str(a.db_path) for a in cfg.accounts}), 2)
        self.assertEqual(len({str(a.db_path.parent) for a in cfg.accounts}), 1)


class TestAccounts(unittest.TestCase):
    """One account by default; `accounts:` turns it into a list.

    The hard rule from the plan is that a Proton session, a staging subtree
    and an Immich key travel as one object. These tests are mostly about the
    ways two accounts must not overlap -- the failure mode is uploading one
    person's photos into the other's library.
    """

    def build(self, entries=None, **overrides):
        data = copy.deepcopy(DEFAULTS)
        data["immich"]["url"] = "http://vm:2283/api"
        data["immich"]["api_key"] = "k"
        if entries is not None:
            data["accounts"] = entries
        for dotted, value in overrides.items():
            node = data
            parts = dotted.replace("__", ".").split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        return Config(data)

    def entry(self, name, **extra):
        base = {"name": name,
                "staging_dir": f"/mnt/immich/staging/{name}",
                "proton_cache_dir": f"/mnt/immich/staging/.proton/{name}",
                "immich_api_key_file": f"/etc/pis/{name}.key"}
        base.update(extra)
        return base

    # -- the single-account case ------------------------------------------
    def test_a_bare_config_is_one_account(self):
        cfg = self.build()
        self.assertEqual([a.account_name for a in cfg.accounts], ["default"])
        self.assertEqual(cfg.account(None).account_name, "default")

    def test_the_default_accounts_paths_keep_their_v1_names(self):
        """An existing deployment's lock path and HA sensors must survive."""
        account = self.build().account(None)
        self.assertEqual(account.lock_path.name, "sync.lock")
        self.assertEqual(account.status_path.name, "status.json")

    def test_account_name_can_be_set(self):
        cfg = self.build(account__name="david")
        self.assertEqual(cfg.account(None).account_name, "david")
        self.assertEqual(cfg.account(None).lock_path.name, "sync-david.lock")
        self.assertEqual(cfg.account(None).status_path.name, "status-david.json")

    # -- the list case ----------------------------------------------------
    def test_shorthand_keys_expand_to_config_paths(self):
        cfg = self.build([self.entry("david", proton_root="/my-files/Pics",
                                     album_name="Mine",
                                     delete_action="execute")])
        david = cfg.account("david")
        self.assertEqual(david.get("proton.roots"), ["/my-files/Pics"])
        self.assertEqual(david.get("immich.album_name"), "Mine")
        self.assertEqual(david.delete_action, "execute")
        self.assertEqual(str(david.staging), "/mnt/immich/staging/david")

    def test_a_nested_section_works_too(self):
        cfg = self.build([{"name": "d", "proton": {"roots": ["/a"]},
                           "staging": {"root": "/s/d"}}])
        self.assertEqual(cfg.account("d").get("proton.roots"), ["/a"])
        self.assertEqual(str(cfg.account("d").staging), "/s/d")

    def test_global_settings_are_inherited(self):
        cfg = self.build([self.entry("david"), self.entry("mirjam")],
                         immich__url="http://vm:2283/api")
        for account in cfg.accounts:
            self.assertEqual(account.get("immich.url"), "http://vm:2283/api")
            self.assertEqual(account.get("proton.backend"), "proton-cli")

    def test_every_account_gets_its_own_database(self):
        """One file per pipeline. Each ingesting process writes only its own,
        so two pipelines never contend for a write lock and a bug in one
        cannot reach the other's rows."""
        cfg = self.build([self.entry("david"), self.entry("mirjam")])
        paths = {str(a.db_path) for a in cfg.accounts}
        self.assertEqual(paths, {"/mnt/immich/staging/.state/david.sqlite",
                                 "/mnt/immich/staging/.state/mirjam.sqlite"})

    def test_the_databases_share_one_directory(self):
        """So the UI can combine them from a single read-only mount."""
        cfg = self.build([self.entry("david"), self.entry("mirjam")])
        self.assertEqual({str(a.db_path.parent) for a in cfg.accounts},
                         {"/mnt/immich/staging/.state"})
        self.assertEqual(sorted(cfg.db_paths()), ["david", "mirjam"])

    def test_each_account_gets_its_own_lock_and_status_file(self):
        cfg = self.build([self.entry("david"), self.entry("mirjam")])
        self.assertEqual({a.lock_path.name for a in cfg.accounts},
                         {"sync-david.lock", "sync-mirjam.lock"})
        self.assertEqual({a.status_path.name for a in cfg.accounts},
                         {"status-david.json", "status-mirjam.json"})

    def test_naming_an_account_selects_it(self):
        cfg = self.build([self.entry("david"), self.entry("mirjam")])
        self.assertEqual(cfg.account("mirjam").account_name, "mirjam")

    def test_an_ambiguous_selection_is_refused_with_the_names(self):
        cfg = self.build([self.entry("david"), self.entry("mirjam")])
        with self.assertRaises(ConfigError) as ctx:
            cfg.account(None)
        self.assertIn("--account", str(ctx.exception))
        self.assertIn("david", str(ctx.exception))

    def test_an_unknown_account_lists_what_exists(self):
        cfg = self.build([self.entry("david")])
        with self.assertRaises(ConfigError) as ctx:
            cfg.account("nobody")
        self.assertIn("david", str(ctx.exception))

    # -- the hard rule ----------------------------------------------------
    def test_a_clean_two_account_config_validates(self):
        cfg = self.build([self.entry("david"), self.entry("mirjam")])
        self.assertEqual(cfg.validate(), [])

    def test_a_per_account_key_file_is_not_a_missing_key(self):
        """The base config has no immich.api_key at all when each account
        carries its own file, and that is correct, not an error."""
        data = copy.deepcopy(DEFAULTS)
        data["immich"]["url"] = "http://vm:2283/api"
        data["accounts"] = [self.entry("david"), self.entry("mirjam")]
        self.assertEqual(Config(data).validate(), [])

    def test_a_shared_staging_root_is_refused(self):
        cfg = self.build([self.entry("david", staging_dir="/shared"),
                          self.entry("mirjam", staging_dir="/shared")])
        self.assertTrue(any("share staging.root" in p for p in cfg.validate()),
                        cfg.validate())

    def test_a_shared_proton_cache_dir_is_refused(self):
        """Two accounts in one cache dir collide on the Proton session."""
        cfg = self.build([self.entry("david", proton_cache_dir="/c"),
                          self.entry("mirjam", proton_cache_dir="/c")])
        self.assertTrue(any("share proton.cache_dir" in p for p in cfg.validate()))

    def test_a_shared_immich_key_is_refused(self):
        cfg = self.build([self.entry("david", immich_api_key_file="/k"),
                          self.entry("mirjam", immich_api_key_file="/k")])
        self.assertTrue(any("share one Immich API key" in p
                            for p in cfg.validate()), cfg.validate())

    def test_an_env_key_inherited_by_both_accounts_is_refused(self):
        """IMMICH_API_KEY applies to the base config, so with no per-account
        key files it silently becomes one key for everyone."""
        data = copy.deepcopy(DEFAULTS)
        data["immich"]["url"] = "http://vm:2283/api"
        data["immich"]["api_key"] = "from-env"
        data["accounts"] = [
            {"name": "david", "staging_dir": "/a", "proton_cache_dir": "/ca"},
            {"name": "mirjam", "staging_dir": "/b", "proton_cache_dir": "/cb"},
        ]
        self.assertTrue(any("share one Immich API key" in p
                            for p in Config(data).validate()))

    def test_problems_inside_an_account_name_that_account(self):
        cfg = self.build([self.entry("david", delete_action="nuke"),
                          self.entry("mirjam")])
        problems = cfg.validate()
        self.assertTrue(any(p.startswith("accounts[david]:") for p in problems),
                        problems)

    def test_an_unnamed_or_duplicate_account_is_refused(self):
        for entries, needle in (
            ([self.entry("d"), {"staging_dir": "/b"}], "has no name"),
            ([self.entry("d"), self.entry("d")], "duplicate account name"),
            ("not-a-list", "must be a list"),
        ):
            with self.subTest(needle=needle):
                problems = self.build(entries).validate()
                self.assertTrue(any(needle in p for p in problems), problems)

    def test_a_scalar_in_the_list_explains_the_yaml_parser_limit(self):
        problems = self.build(["david", "mirjam"]).validate()
        self.assertTrue(any("PyYAML" in p for p in problems), problems)

    def test_an_api_key_file_is_read_on_demand(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "david.key"
            key.write_text("secret-key\n")
            cfg = self.build([self.entry("david", immich_api_key_file=str(key))])
            account = cfg.account("david")
            self.assertEqual(account.immich_api_key(), "secret-key")
            # It must not be cached back into the config data, which gets
            # logged and serialised.
            self.assertNotIn("secret-key", str(account.data))

    def test_an_unreadable_key_file_is_a_clear_error(self):
        cfg = self.build([self.entry("david",
                                     immich_api_key_file="/nope/david.key")])
        with self.assertRaises(ConfigError) as ctx:
            cfg.account("david").immich_api_key()
        self.assertIn("api_key_file", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
