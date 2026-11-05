"""Isolated tests for the current split BIRD configuration workflow."""

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config_generator import (  # noqa: E402
    build_bgp_blocks,
    build_device_block,
    build_direct_block,
    build_static_block,
    generate_split,
    main_config_status,
    migrate_main_config,
    render_managed_main_block,
)
from app.config_manager import ConfigManager  # noqa: E402
from app.diff_engine import DiffEngine  # noqa: E402
from app.syntax_validator import SyntaxValidator  # noqa: E402


FAKE_BIRDC = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_BIRDC_ARGS"
if [ -n "$FAKE_BIRDC_FAIL" ]; then
  printf 'Unable to connect to server control socket: refused\\n' >&2
  exit 1
fi
if [ -n "$FAKE_BIRDC_STATUS" ]; then
  printf '%s\\n' "$FAKE_BIRDC_STATUS"
fi
exit 0
"""


@pytest.fixture
def fake_birdc(tmp_path, monkeypatch):
    script = tmp_path / "birdc"
    script.write_text(FAKE_BIRDC)
    script.chmod(0o755)
    args_file = tmp_path / "birdc.args"
    monkeypatch.setenv("FAKE_BIRDC_ARGS", str(args_file))
    return script, args_file


@pytest.fixture
def config_manager(tmp_path):
    return ConfigManager(
        live_path=str(tmp_path / "live"),
        staged_path=str(tmp_path / "staged"),
        db_session_factory=lambda: None,
    )


def peer(name="peer_lan", neighbor="192.168.0.1", asn="64999", local_as="65500",
         export_all=False):
    return {
        "name": name,
        "local_as": local_as,
        "neighbor": neighbor,
        "neighbor_as": asn,
        "export_all": export_all,
    }


def networks(*items):
    return list(items)


def community(community_id=7, enabled=True):
    return {
        "id": community_id,
        "name": f"65500:{community_id}",
        "values": "",
        "description": "",
        "enabled": enabled,
    }


class TestBirdReload:
    def test_reload_uses_configure_without_socket(self, fake_birdc, config_manager, monkeypatch):
        script, args_file = fake_birdc
        monkeypatch.setenv("BIRDC_BIN_PATH", str(script))
        monkeypatch.delenv("BIRD_SOCKET", raising=False)

        assert config_manager._attempt_bird_reload() == (True, False, None)
        assert args_file.read_text().splitlines() == ["configure"]

    def test_reload_uses_configured_socket(self, fake_birdc, config_manager, monkeypatch):
        script, args_file = fake_birdc
        monkeypatch.setenv("BIRDC_BIN_PATH", str(script))
        monkeypatch.setenv("BIRD_SOCKET", "/run/bird/test.ctl")

        assert config_manager._attempt_bird_reload() == (True, False, None)
        assert args_file.read_text().splitlines() == [
            "-s", "/run/bird/test.ctl", "configure"
        ]

    def test_reload_failure_is_degraded(self, fake_birdc, config_manager, monkeypatch):
        script, _ = fake_birdc
        monkeypatch.setenv("BIRDC_BIN_PATH", str(script))
        monkeypatch.setenv("FAKE_BIRDC_FAIL", "1")

        success, warning, detail = config_manager._attempt_bird_reload()

        assert success is False
        assert warning is True
        assert "refused" in detail

    def test_reload_state_is_persisted(self, fake_birdc, config_manager, monkeypatch):
        script, _ = fake_birdc
        monkeypatch.setenv("BIRDC_BIN_PATH", str(script))
        monkeypatch.setenv("FAKE_BIRDC_FAIL", "1")
        config_manager._write_apply_state(True, "reload failed")

        state = config_manager._read_apply_state()

        assert state["reload_warning"] is True
        assert state["reload_detail"] == "reload failed"

    def test_status_timestamps_are_parsed_from_birdc(
        self, fake_birdc, config_manager, monkeypatch
    ):
        script, args_file = fake_birdc
        monkeypatch.setenv("BIRDC_BIN_PATH", str(script))
        monkeypatch.setenv(
            "FAKE_BIRDC_STATUS",
            "BIRD 3.2.0 ready.\n"
            "Last reboot on 2026-09-25 01:02:32.970\n"
            "Last reconfiguration on 2026-09-26 01:17:01.767\n"
            "Daemon is up and running",
        )

        assert config_manager._bird_status_timestamps() == (
            "25.09.2026 01:02:32",
            "26.09.2026 01:17:01",
        )
        assert args_file.read_text().splitlines() == ["show status"]

    def test_status_timestamps_are_none_when_birdc_fails(
        self, fake_birdc, config_manager, monkeypatch
    ):
        script, _args_file = fake_birdc
        monkeypatch.setenv("BIRDC_BIN_PATH", str(script))
        monkeypatch.setenv("FAKE_BIRDC_FAIL", "1")

        assert config_manager._bird_status_timestamps() == (None, None)


class TestSplitGenerator:
    def test_split_output_has_separate_files(self, tmp_path):
        staged = tmp_path / "staged"
        generate_split(
            networks(
                {"prefix": "203.0.113.0/24", "community_ids": [7], "enabled": True}
            ),
            [community()],
            staged_dir=str(staged), managed_include_dir=str(tmp_path / "live"),
            router_id="203.0.113.254",
            local_as="64512",
        )

        assert (staged / "networks.conf").exists()
        assert (staged / "routes.conf").exists()
        assert (staged / "bird-manager.conf").exists()
        assert (staged / "export.conf").exists()
        assert "protocol static managed_static_routes" in (staged / "routes.conf").read_text()
        assert "protocol static managed_static_routes" not in (staged / "networks.conf").read_text()
        # The per-Community policy files are gone for good: nothing referenced
        # their export_policy_<id> filters, they were half the bundle in dead
        # weight, and the export filters now live in export.conf.
        assert not list(staged.glob("policy_*.conf"))
        assert "export filter" not in (staged / "networks.conf").read_text()
        assert "COMM_" not in (staged / "bird-manager.conf").read_text()

    def test_default_community_zero_creates_export_policy(self, tmp_path):
        staged = tmp_path / "staged"
        generate_split(
            networks({"prefix": "203.0.113.0/24", "community_ids": [2], "enabled": True}),
            [{
                "id": 2,
                "name": "65534",
                "values": "0",
                "description": "default",
                "enabled": True,
            }],
            staged_dir=str(staged), managed_include_dir=str(tmp_path / "live"),
            router_id="203.0.113.254",
            local_as="64512",
            bgp_peers=[peer()],
        )

        # peer() announces 65500 but the only community is 65534, so its own
        # filter must stay empty rather than fall back to the whole table.
        own = (staged / "export.conf").read_text()
        assert "filter export_to_peer_lan {" in own
        block = own.split("filter export_to_peer_lan {")[1].split("\n}")[0]
        assert "bgp_community.add" not in block
        assert block.rstrip().endswith("reject;")
        assert "filter export_own_networks {" not in own

    def test_community_zero_value_reaches_combined_filter(self, tmp_path):
        """`values: "0"` is a real community value, not a missing one."""
        staged = tmp_path / "staged"
        generate_split(
            networks({"prefix": "203.0.113.0/24", "community_ids": [2], "enabled": True}),
            [{
                "id": 2,
                "name": "65534",
                "values": "0",
                "description": "default",
                "enabled": True,
            }],
            staged_dir=str(staged), managed_include_dir=str(tmp_path / "live"),
            router_id="203.0.113.254",
            local_as="64512",
            bgp_peers=[peer(export_all=True)],
        )

        assert "export filter export_own_networks;" in (staged / "core.conf").read_text()
        combined = (staged / "export.conf").read_text()
        assert "filter export_own_networks {" in combined
        assert "bgp_community.add((65534, 0));" in combined
        assert combined.rstrip().endswith('reject;\n}')

    def test_disabled_network_is_absent(self, tmp_path):
        staged = tmp_path / "staged"
        generate_split(
            networks(
                {"prefix": "203.0.113.0/24", "community_ids": [7], "enabled": False},
                {"prefix": "198.51.100.0/24", "community_ids": [7], "enabled": True},
            ),
            [community()],
            staged_dir=str(staged), managed_include_dir=str(tmp_path / "live"),
            router_id="203.0.113.254",
            local_as="64512",
            bgp_peers=[peer()],
        )

        routes = (staged / "routes.conf").read_text()
        exported = (staged / "export.conf").read_text()
        assert "203.0.113.0/24" not in routes
        assert "198.51.100.0/24" in routes
        assert "203.0.113.0/24" not in exported
        assert "198.51.100.0/24" in exported

    def test_disabled_community_has_no_policy_file(self, tmp_path):
        staged = tmp_path / "staged"
        generate_split(
            networks({"prefix": "203.0.113.0/24", "community_ids": [7], "enabled": True}),
            [community(enabled=False)],
            staged_dir=str(staged), managed_include_dir=str(tmp_path / "live"),
            router_id="203.0.113.254",
            local_as="64512",
            bgp_peers=[peer()],
        )

        assert not (staged / "policy_7.conf").exists()
        assert "protocol static managed_static_routes" in (staged / "routes.conf").read_text()
        # A disabled Community contributes no pairs, so the peer exporting that
        # ASN must announce nothing.
        block = (staged / "export.conf").read_text().split("filter export_to_peer_lan {")[1]
        assert "bgp_community.add" not in block.split("\n}")[0]

    def test_bgp_channel_uses_single_export_filter(self, tmp_path, monkeypatch):
        """A BIRD channel keeps one export filter; extra lines are overwritten.

        Regression for the silent route loss: one "export filter" line per
        Community meant that only the last Community was ever exported.
        """
        monkeypatch.setattr("app.config_generator._lan_interface", lambda: "eth0")
        staged = tmp_path / "staged"
        generate_split(
            networks({"prefix": "203.0.113.0/24", "community_ids": [7], "enabled": True}),
            [community()],
            staged_dir=str(staged), managed_include_dir=str(tmp_path / "live"),
            router_id="203.0.113.254",
            local_as="64512",
            bgp_peers=[peer()],
        )

        content = (staged / "core.conf").read_text()
        assert content.count("export filter") == 1
        # The channel names the peer's own filter, so the block and the
        # generated filter cannot drift apart.
        assert "export filter export_to_peer_lan;" in content
        assert "export filter export_policy_" not in content
        assert "export filter" not in (staged / "networks.conf").read_text()
        assert "filter export_to_peer_lan {" in (staged / "export.conf").read_text()

    def test_peers_of_one_asn_share_prefixes_of_that_asn_only(self, tmp_path, monkeypatch):
        """Two sessions on one MikroTik must carry the same prefix set.

        This is the whole point of the per-peer layout: both BIRD local ASes are
        64600, so both filters carry exactly the 64600 prefixes and neither
        leaks the 65500 ones.
        """
        monkeypatch.setattr("app.config_generator._lan_interface", lambda: "eth0")
        staged = tmp_path / "staged"
        peers = [
            peer("first", neighbor="192.168.0.1", asn="64998", local_as="64600"),
            peer("second", neighbor="192.168.0.2", asn="64999", local_as="64600"),
        ]
        generate_split(
            networks(
                {"prefix": "203.0.113.0/24", "community_ids": [7], "enabled": True},
                {"prefix": "198.51.100.0/24", "community_ids": [8], "enabled": True},
            ),
            [community(), {"id": 8, "name": "64600:8", "values": "", "description": "",
                           "enabled": True}],
            staged_dir=str(staged), managed_include_dir=str(tmp_path / "live"),
            router_id="203.0.113.254",
            local_as="64512",
            bgp_peers=peers,
        )

        exported = (staged / "export.conf").read_text()
        blocks = {
            name: exported.split(f"filter export_to_{name} {{")[1].split("\n}")[0]
            for name in ("first", "second")
        }
        assert blocks["first"] == blocks["second"]
        assert "198.51.100.0/24" in blocks["first"]
        assert "203.0.113.0/24" not in blocks["first"]
        assert "65500" not in blocks["first"]
        assert "filter export_own_networks {" not in exported
        content = (staged / "core.conf").read_text()
        assert "export filter export_to_first;" in content
        assert "export filter export_to_second;" in content
        assert content.count("export filter") == 2

    def test_unknown_asn_peer_exports_nothing(self, tmp_path, monkeypatch):
        """No community carries the peer's ASN: it must export nothing."""
        monkeypatch.setattr("app.config_generator._lan_interface", lambda: "eth0")
        staged = tmp_path / "staged"
        generate_split(
            networks({"prefix": "203.0.113.0/24", "community_ids": [7], "enabled": True}),
            [community()],
            staged_dir=str(staged), managed_include_dir=str(tmp_path / "live"),
            router_id="203.0.113.254",
            local_as="64512",
            bgp_peers=[peer(local_as="64997")],
        )

        exported = (staged / "export.conf").read_text()
        block = exported.split("filter export_to_peer_lan {")[1].split("\n}")[0]
        assert "bgp_community.add" not in block
        assert block.rstrip().endswith("reject;")
        assert "no communities for this ASN" in exported
        # The channel still references the filter, otherwise BIRD would refuse
        # to start on an undefined filter.
        assert "export filter export_to_peer_lan;" in (staged / "core.conf").read_text()

    def test_export_all_peer_gets_combined_filter(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.config_generator._lan_interface", lambda: "eth0")
        staged = tmp_path / "staged"
        generate_split(
            networks(
                {"prefix": "203.0.113.0/24", "community_ids": [7], "enabled": True},
                {"prefix": "198.51.100.0/24", "community_ids": [8], "enabled": True},
                {"prefix": "192.0.2.0/24", "community_ids": [7, 8], "enabled": True},
            ),
            [community(), {"id": 8, "name": "65000:8", "values": "", "description": "",
                           "enabled": True}],
            staged_dir=str(staged), managed_include_dir=str(tmp_path / "live"),
            router_id="203.0.113.254",
            local_as="64512",
            bgp_peers=[peer(export_all=True)],
        )

        content = (staged / "core.conf").read_text()
        assert content.count("export filter") == 1
        assert "export filter export_own_networks;" in content
        combined = (staged / "export.conf").read_text()
        assert "bgp_community.add((65500, 7));" in combined
        assert "bgp_community.add((65000, 8));" in combined
        block = combined.split("if net = 192.0.2.0/24 then {")[1].split("}")[0]
        assert "bgp_community.add((65500, 7));" in block
        assert "bgp_community.add((65000, 8));" in block
        assert "net ~ [ 192.168.0.1/32 ]" in combined
        assert combined.rstrip().endswith('reject;\n}')

    def test_one_export_filter_merges_every_community(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.config_generator._lan_interface", lambda: "eth0")
        staged = tmp_path / "staged"
        generate_split(
            networks(
                {"prefix": "203.0.113.0/24", "community_ids": [7], "enabled": True},
                {"prefix": "198.51.100.0/24", "community_ids": [8], "enabled": True},
                {"prefix": "192.0.2.0/24", "community_ids": [7, 8], "enabled": True},
            ),
            [
                community(),
                {
                    "id": 8, "name": "65000:8", "values": "",
                    "description": "second", "enabled": True,
                },
            ],
            staged_dir=str(staged), managed_include_dir=str(tmp_path / "live"),
            router_id="203.0.113.254",
            local_as="64512",
            bgp_peers=[peer()],
        )

        exported = (staged / "export.conf").read_text()
        block = exported.split("filter export_to_peer_lan {")[1].split("\n}")[0]
        # 65500:7 belongs to the peer's own ASN, 65000:8 does not.
        assert "bgp_community.add((65500, 7));" in block
        assert "65000" not in block
        # The prefix carrying both Communities gets only its own ASN's pair,
        # never the other peer's value.
        dual = block.split("if net = 192.0.2.0/24 then {")[1].split("}")[0]
        assert "bgp_community.add((65500, 7));" in dual
        assert "(65000, 8)" not in dual
        assert "net ~ [ 192.168.0.1/32 ]" in exported
        assert block.rstrip().endswith("reject;")

    def test_export_conf_is_included_before_core(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.config_generator._lan_interface", lambda: "eth0")
        staged = tmp_path / "staged"
        generate_split(
            networks({"prefix": "203.0.113.0/24", "community_ids": [7], "enabled": True}),
            [community()],
            staged_dir=str(staged), managed_include_dir=str(tmp_path / "live"),
            router_id="203.0.113.254",
            local_as="64512",
            bgp_peers=[peer()],
        )

        entrypoint = (staged / "bird-manager.conf").read_text()
        order = [
            line for line in entrypoint.splitlines() if line.startswith("include ")
        ]
        names = (
            'networks.conf', 'export_peer_peer_lan.conf', 'routes_65500_7.conf', 'core.conf',
        )
        positions = {
            name: next(
                index for index, line in enumerate(order) if line.endswith(f'/{name}";')
            )
            for name in names
        }
        assert positions['networks.conf'] < positions['export_peer_peer_lan.conf']
        assert positions['export_peer_peer_lan.conf'] < positions['routes_65500_7.conf']
        assert positions['routes_65500_7.conf'] < positions['core.conf']
        assert (staged / "export_peer_peer_lan.conf").is_file()

    def test_real_bird_parses_split_bundle(self, tmp_path, monkeypatch):
        if shutil.which("bird") is None:
            pytest.skip("bird binary not installed")
        live = tmp_path / "live"
        staged = tmp_path / "staged"
        live.mkdir()
        staged.mkdir()
        main = tmp_path / "bird.conf"
        main.write_text(render_managed_main_block(live) + "\n")
        monkeypatch.setenv("BIRD_MAIN_CONFIG", str(main))
        monkeypatch.setenv("BIRD_BIN_PATH", str(tmp_path / "bird-missing"))
        monkeypatch.setenv("BIRDC_BIN_PATH", "/bin/true")
        generate_split(
            networks({"prefix": "203.0.113.0/24", "community_ids": [7], "enabled": True}),
            [community()],
            staged_dir=str(staged), managed_include_dir=str(tmp_path / "live"),
            router_id="203.0.113.254",
            local_as="64512",
            bgp_peers=[peer()],
        )
        result = ConfigManager(str(live), str(staged), lambda: None).publish_staged_generated()
        assert result["status"] == "success"
        assert subprocess.run(
            [shutil.which("bird"), "-p", "-c", str(main)],
            capture_output=True,
            text=True,
            timeout=30,
        ).returncode == 0

    def test_legacy_main_migration_preserves_operator_content(self, tmp_path):
        live = tmp_path / "live"
        legacy = (
            "# operator header\n"
            "# BEGIN bird-manager managed includes\n"
            f'include "{live / "networks.conf"}";\n'
            f'include "{live / "routes.conf"}";\n'
            "# END bird-manager managed includes\n"
            "# operator footer\n"
        )

        migrated, changed = migrate_main_config(legacy, live)
        stable, detail = main_config_status(migrated, live)

        assert changed is True
        assert stable is True, detail
        assert "# operator header" in migrated
        assert "# operator footer" in migrated
        assert f'include "{live / "bird-manager.conf"}";' in migrated
        assert f'include "{live / "networks.conf"}";' not in migrated
        assert migrate_main_config(migrated, live) == (migrated, False)


class TestManagedBundle:
    def test_publish_updates_entrypoint_without_changing_main(self, tmp_path, monkeypatch):
        live = tmp_path / "live"
        staged = tmp_path / "staged"
        live.mkdir()
        staged.mkdir()
        main = tmp_path / "bird.conf"
        main.write_text(
            "# operator comments\n"
            + render_managed_main_block(live)
            + "\n"
        )
        monkeypatch.setenv("BIRD_MAIN_CONFIG", str(main))
        monkeypatch.setenv("BIRD_BIN_PATH", "/usr/sbin/bird")
        monkeypatch.setenv("BIRDC_BIN_PATH", "/bin/true")
        main.chmod(0o444)
        main_before = main.read_text()
        generate_split(
            networks({"prefix": "203.0.113.0/24", "community_ids": [7], "enabled": True}),
            [community()],
            staged_dir=str(staged), managed_include_dir=str(tmp_path / "live"),
            router_id="203.0.113.254",
            local_as="64512",
        )

        manager = ConfigManager(str(live), str(staged), lambda: None)
        result = manager.publish_staged_generated()

        assert result["status"] == "success"
        assert live.stat().st_mode & stat.S_ISGID
        overview = manager.get_config_overview()
        assert "bird_conf" not in overview
        assert overview["managed_conf"]["adds"] == 0
        assert overview["managed_conf"]["dels"] == 0
        assert overview["core"]["adds"] == 0
        assert overview["core"]["dels"] == 0
        assert all(row["adds"] == 0 and row["dels"] == 0 for row in overview["summary"])
        assert main.read_text() == main_before
        content = (live / "bird-manager.conf").read_text()
        assert content.index("networks.conf") < content.index("routes_65500_7.conf")
        assert content.index("routes_65500_7.conf") < content.index("core.conf")
        assert (live / "export.conf").exists()
        assert (live / "routes.conf").exists()
        assert (live / "core.conf").exists()
        assert not (live / "policy_7.conf").exists()

    def test_apply_publishes_full_staged_bundle(self, tmp_path, monkeypatch):
        live = tmp_path / "live"
        staged = tmp_path / "staged"
        live.mkdir()
        staged.mkdir()
        main = tmp_path / "bird.conf"
        main.write_text(render_managed_main_block(live) + "\n")
        monkeypatch.setenv("BIRD_MAIN_CONFIG", str(main))
        monkeypatch.setenv("BIRD_BIN_PATH", str(tmp_path / "bird-missing"))
        monkeypatch.setenv("BIRDC_BIN_PATH", "/bin/true")
        main_before = main.read_text()
        generate_split(
            networks({"prefix": "203.0.113.0/24", "community_ids": [7], "enabled": True}),
            [community()],
            staged_dir=str(staged), managed_include_dir=str(tmp_path / "live"),
            router_id="203.0.113.254",
            local_as="64512",
        )

        manager = ConfigManager(str(live), str(staged), lambda: None)
        assert not (live / "export.conf").exists()
        result = manager.apply_config("networks.conf")

        assert result["status"] == "success"
        assert (live / "networks.conf").exists()
        assert (live / "export.conf").exists()
        assert (live / "routes.conf").exists()
        assert (live / "core.conf").exists()
        assert (live / "bird-manager.conf").exists()
        assert 'include "' + str(live / "routes_65500_7.conf") + '";' in (live / "bird-manager.conf").read_text()
        assert main.read_text() == main_before
        overview = manager.get_config_overview()
        assert overview["managed_conf"]["adds"] == 0
        assert overview["managed_conf"]["dels"] == 0
        assert all(row["adds"] == 0 and row["dels"] == 0 for row in overview["summary"])

    def test_stage_regeneration_does_not_change_live(self, tmp_path, monkeypatch):
        live = tmp_path / "live"
        staged = tmp_path / "staged"
        live.mkdir()
        staged.mkdir()
        main = tmp_path / "bird.conf"
        main.write_text(render_managed_main_block(live) + "\n")
        monkeypatch.setenv("BIRD_MAIN_CONFIG", str(main))
        monkeypatch.setenv("BIRD_BIN_PATH", str(tmp_path / "bird-missing"))
        monkeypatch.setenv("BIRDC_BIN_PATH", "/bin/true")
        generate_split(
            networks(
                {"prefix": "203.0.113.0/24", "community_ids": [7], "enabled": True},
            ),
            [community(enabled=False)],
            staged_dir=str(staged), managed_include_dir=str(tmp_path / "live"),
            router_id="203.0.113.254",
            local_as="64512",
        )

        manager = ConfigManager(str(live), str(staged), lambda: None)
        overview = manager.get_config_overview()

        assert not (live / "routes.conf").exists()
        assert not (live / "core.conf").exists()
        assert not (live / "export.conf").exists()
        assert "203.0.113.0/24" in (staged / "routes.conf").read_text()
        # A file that is new in staged adds every one of its lines. Asserting
        # exact counts keeps the diff counters honest; a "> 0" check passed
        # even when the diff body was glued into a single line.
        for key, name in (
            ("networks", "networks.conf"),
            ("managed_conf", "bird-manager.conf"),
            ("core", "core.conf"),
        ):
            assert overview[key]["adds"] == len(
                (staged / name).read_text().splitlines()
            ), name
            assert overview[key]["dels"] == 0, name
        routes_row = next(
            row for row in overview["summary"] if row["filename"] == "routes.conf"
        )
        assert routes_row["adds"] == len((staged / "routes.conf").read_text().splitlines())
        assert routes_row["dels"] == 0
        assert not any(row["filename"] == "policy_7.conf" for row in overview["summary"])
        assert any(row["filename"] == "export.conf" for row in overview["summary"])

    def test_apply_then_rollback_restores_previous_publish(self, tmp_path, monkeypatch):
        """Rollback comes from the publish snapshot, not a live-directory copy."""
        live = tmp_path / "live"
        staged = tmp_path / "staged"
        live.mkdir()
        staged.mkdir()
        main = tmp_path / "bird.conf"
        main.write_text("operator config\n")
        (live / "networks.conf").write_text("# zero\n")
        (staged / "networks.conf").write_text("# one\n")
        monkeypatch.setenv("BIRD_MAIN_CONFIG", str(main))
        monkeypatch.setenv("BIRD_BIN_PATH", str(tmp_path / "bird-missing"))
        monkeypatch.setenv("BIRDC_BIN_PATH", "/bin/true")
        manager = ConfigManager(str(live), str(staged), lambda: None)
        snapshot = tmp_path / "live-bak" / "networks.conf.bak"

        assert manager.apply_config("networks.conf")["status"] == "success"
        assert (live / "networks.conf").read_text() == "# one\n"
        assert snapshot.read_text() == "# zero\n"

        (staged / "networks.conf").write_text("# two\n")
        assert manager.apply_config("networks.conf")["status"] == "success"
        assert (live / "networks.conf").read_text() == "# two\n"
        assert manager.rollback_last_apply("networks.conf")["status"] == "success"
        assert (live / "networks.conf").read_text() == "# one\n"
        # The rollback source lives outside the BIRD include directory.
        assert not (live / "networks.conf.bak").exists()
        assert not (tmp_path / "last-published").exists()

    def test_publish_keeps_live_dir_clean_and_snapshots_in_state_dir(
        self, tmp_path, monkeypatch
    ):
        live = tmp_path / "live"
        staged = tmp_path / "staged"
        live.mkdir()
        staged.mkdir()
        main = tmp_path / "bird.conf"
        main.write_text(render_managed_main_block(live) + "\n")
        monkeypatch.setenv("BIRD_MAIN_CONFIG", str(main))
        monkeypatch.setenv("BIRD_BIN_PATH", str(tmp_path / "bird-missing"))
        monkeypatch.setenv("BIRDC_BIN_PATH", "/bin/true")
        generate_split(
            networks({"prefix": "203.0.113.0/24", "community_ids": [7], "enabled": True}),
            [community()],
            staged_dir=str(staged), managed_include_dir=str(live),
            router_id="203.0.113.254", local_as="64512",
            bgp_peers=[peer()],
        )

        manager = ConfigManager(str(live), str(staged), lambda: None)
        assert manager.publish_staged_generated()["status"] == "success"
        assert sorted(os.listdir(live)) == [
            "bird-manager.conf", "core.conf", "export.conf", "export_peer_peer_lan.conf",
            "networks.conf", "routes.conf", "routes_65500_7.conf",
        ]
        snapshot_dir = tmp_path / "live-bak"
        assert not list(snapshot_dir.glob("*.bak"))

        generate_split(
            networks({"prefix": "203.0.114.0/24", "community_ids": [7], "enabled": True}),
            [community()],
            staged_dir=str(staged), managed_include_dir=str(live),
            router_id="203.0.113.254", local_as="64512",
            bgp_peers=[peer()],
        )
        assert manager.publish_staged_generated()["status"] == "success"
        assert not list(live.glob("*.bak"))
        assert not list(live.glob("*.tmp"))
        snapshot = (snapshot_dir / "export.conf.bak").read_text()
        assert "203.0.113.0/24" in snapshot
        assert "203.0.114.0/24" in (live / "export.conf").read_text()
        assert manager.rollback_last_apply("networks.conf")["status"] == "success"
        assert not list(live.glob("*.bak"))
        assert "203.0.113.0/24" in (live / "export.conf").read_text()
        assert sorted(os.listdir(live)) == [
            "bird-manager.conf", "core.conf", "export.conf", "export_peer_peer_lan.conf",
            "networks.conf", "routes.conf", "routes_65500_7.conf",
        ]

    def test_legacy_live_backups_are_migrated_out_of_live_dir(self, tmp_path, monkeypatch):
        live = tmp_path / "live"
        staged = tmp_path / "staged"
        live.mkdir()
        staged.mkdir()
        main = tmp_path / "bird.conf"
        main.write_text(render_managed_main_block(live) + "\n")
        (live / "networks.conf").write_text("# current\n")
        (live / "networks.conf.bak").write_text("# legacy\n")
        (live / "routes.conf.tmp").write_text("# partial\n")
        monkeypatch.setenv("BIRD_MAIN_CONFIG", str(main))
        monkeypatch.setenv("BIRD_BIN_PATH", str(tmp_path / "bird-missing"))
        monkeypatch.setenv("BIRDC_BIN_PATH", "/bin/true")

        manager = ConfigManager(str(live), str(staged), lambda: None)

        assert os.listdir(live) == ["networks.conf"]
        assert (tmp_path / "live-bak" / "networks.conf.bak").read_text() == "# legacy\n"
        manager.rollback_last_apply("networks.conf")
        assert (live / "networks.conf").read_text() == "# legacy\n"
        assert os.listdir(live) == ["networks.conf"]

    def test_generated_files_are_listed_in_diff_engine(self, tmp_path):
        """Every bundle member is diffable; unrelated files stay out.

        The listing used to be pinned to networks.conf, which made an empty
        diff mean "nothing changed" for a bundle whose prefixes live in
        routes.conf and whose combined filter lives in export.conf.
        """
        live = tmp_path / "live"
        staged = tmp_path / "staged"
        live.mkdir()
        staged.mkdir()
        for directory in (live, staged):
            (directory / "networks.conf").write_text("base")
            (directory / "routes.conf").write_text("routes")
            (directory / "policy_7.conf").write_text("policy")
        (live / "unrelated.conf").write_text("not ours")
        (live / "notes.txt").write_text("not ours")

        engine = DiffEngine(str(live), str(staged))

        assert engine._all_filenames() == {
            "networks.conf", "routes.conf", "policy_7.conf",
        }
        assert engine.get_all_diffs() == {}
        assert engine.get_file_count_diff() == {
            "staged_only": 0, "live_only": 0, "common": 3, "modified": 0,
        }

        (staged / "routes.conf").write_text("routes\nroute 10.0.0.0/8 via \"x\";\n")
        diffs = engine.get_all_diffs()

        assert list(diffs) == ["routes.conf"]
        assert diffs["routes.conf"].count("\n") == 5
        assert engine.get_file_count_diff()["modified"] == 1

    def test_publish_failure_does_not_publish_or_reload(self, tmp_path, monkeypatch):
        live = tmp_path / "live"
        staged = tmp_path / "staged"
        live.mkdir()
        staged.mkdir()
        main = tmp_path / "bird.conf"
        main.write_text("# operator config\n" + render_managed_main_block(live) + "\n")
        main_before = main.read_text()
        fake_bird = tmp_path / "bird-invalid"
        fake_bird.write_text("#!/bin/sh\nexit 1\n")
        fake_bird.chmod(0o755)
        monkeypatch.setenv("BIRD_MAIN_CONFIG", str(main))
        monkeypatch.setenv("BIRD_BIN_PATH", str(fake_bird))
        monkeypatch.setenv("BIRDC_BIN_PATH", "/bin/true")
        generate_split(
            networks({"prefix": "203.0.113.0/24", "community_ids": [7], "enabled": True}),
            [community()],
            staged_dir=str(staged), managed_include_dir=str(tmp_path / "live"),
            router_id="203.0.113.254",
            local_as="64512",
        )

        result = ConfigManager(str(live), str(staged), lambda: None).publish_staged_generated()

        assert result["status"] == "error"
        assert not (live / "policy_7.conf").exists()
        assert main.read_text() == main_before


class TestCurrentEditorMerge:
    def test_editor_merge_keeps_only_common_managed_blocks(self, config_manager, monkeypatch):
        monkeypatch.setattr("app.config_generator._lan_interface", lambda: "eth0")
        config_manager.split_mode = True
        merged = config_manager._merge_managed_blocks(
            "# user note\nrouter id 192.0.2.1;\n# Static routes\n",
            [{"prefix": "203.0.113.0/24", "community_ids": [7], "enabled": True}],
            [community()],
            bgp_peers=[peer()],
        )

        assert "# user note" in merged
        assert "router id 192.0.2.1;" in merged
        assert "export filter export_policy_7;" not in merged
        assert "protocol static managed_static_routes" not in merged
        assert "protocol device" not in merged
        assert "protocol direct" not in merged
        assert "protocol bgp" not in merged
        assert "route 203.0.113.0/24" not in merged
        assert merged.count('# Device') == 0
        assert merged.count('# Direct routes') == 0
        assert merged.count('# BGP sessions') == 0


class TestSyntaxValidator:
    def test_bird_error_lines_are_parsed(self):
        assert SyntaxValidator._bird_error_line("path.conf:12:3 syntax error") == 12
        assert SyntaxValidator._bird_error_line("path.conf, line 7: syntax error") == 7
        assert SyntaxValidator._bird_error_line("") is None

    def test_invalid_prefix_is_rejected(self):
        result = SyntaxValidator().validate_content(
            'route 300.1.1.1/33 via "lo";\n', bird_config_path=None
        )
        assert result["valid"] is False
        assert result["error_lines"]

    def test_valid_split_fragment_passes_bird(self, tmp_path):
        if shutil.which("bird") is None:
            pytest.skip("bird binary not installed")
        path = tmp_path / "policy.conf"
        path.write_text(
            "router id 192.0.2.1;\n"
            "protocol device {\n"
            "}\n"
            "filter policy_test {\n"
            "    if source != RTS_STATIC then reject;\n"
            "    reject;\n"
            "}\n"
        )
        result = SyntaxValidator().validate_content(
            path.read_text(), bird_config_path=str(path)
        )
        assert result["valid"] is True, result["errors"]


PROTOCOLS_OUTPUT = """BIRD 3.2.0 ready.
Name       Proto      Table      State  Since         Info
managed_static_routes Static     master4    up     2026-09-25    
  Created:            2026-09-25
  Channel ipv4
    State:          UP
    Import state:   UP
    Export state:   DOWN
    Table:          master4
    Routes:         7818 imported, 0 exported, 7818 preferred

device1    Device     ---        up     2026-09-25    
  Created:            2026-09-25

direct1    Direct     ---        up     2026-09-25    
  Created:            2026-09-25
  Channel ipv4
    State:          UP
    Import state:   UP
    Export state:   DOWN
    Routes:         1 imported, 0 exported, 1 preferred

main_mkt   BGP        ---        start  2026-09-25    Active        Socket: Connection refused
  Created:            2026-09-25
  BGP state:          Active
    Neighbor address: 192.168.0.1
    Neighbor AS:      64999
    Local AS:         65500
    Connect delay:    0.274/5
    Last error:       Socket: Connection refused
  Channel ipv4
    State:          DOWN
    Import state:   DOWN
    Export state:   DOWN
    Table:          master4
    Preference:     100
    Input filter:   REJECT
    Output filter:  export_policy_2

peer_lan   BGP        ---        up     2026-09-26 12:04:11
  Created:            2026-09-26
  BGP state:          Established
    Neighbor address: 10.0.0.9
    Neighbor AS:      65001
    Local AS:         65500
  Channel ipv4
    State:          UP
    Import state:   UP
    Export state:   UP
    Table:          master4
    Routes:         1204 imported, 1180 exported, 1204 preferred

manual_ru  BGP        ---        idle   2026-09-24
  Created:            2026-09-24
  BGP state:          Idle
    Neighbor address: 203.0.113.7
    Neighbor AS:      64512
    Local AS:         65500
  Channel ipv4
    State:          DOWN
"""

ESTABLISHED_PROTOCOLS = """BIRD 3.2.0 ready.
Name       Proto      Table      State  Since         Info
peer_lan   BGP        master4    up     2026-09-26 12:04:11
  BGP state:          Established
    Neighbor address: 10.0.0.9
    Neighbor AS:      65001
    Local AS:         65500
  Channel ipv4
    State:          UP
    Import state:   UP
    Export state:   UP
    Routes:         1204 imported, 1180 exported, 1204 preferred
"""


class TestProtocolsParser:
    def test_classifies_by_proto_column(self):
        records = ConfigManager._parse_protocols_output(PROTOCOLS_OUTPUT)
        kinds = [(r['proto'], r['name']) for r in records]
        assert ('Static', 'managed_static_routes') in kinds
        assert ('Device', 'device1') in kinds
        assert ('Direct', 'direct1') in kinds
        assert ('BGP', 'main_mkt') in kinds
        assert ('BGP', 'peer_lan') in kinds
        assert ('BGP', 'manual_ru') in kinds

    def test_banner_and_header_rows_are_ignored(self):
        records = ConfigManager._parse_protocols_output(PROTOCOLS_OUTPUT)
        names = [r['name'] for r in records]
        assert 'BIRD' not in names
        assert 'Name' not in names
        assert len(names) == 6

    def test_long_protocol_name_keeps_proto_column(self):
        # Name and Proto are separated by a single space here, so splitting on
        # two-or-more spaces would merge the columns.
        records = ConfigManager._parse_protocols_output(PROTOCOLS_OUTPUT)
        static = next(r for r in records if r['name'] == 'managed_static_routes')
        assert static['proto'] == 'Static'
        assert static['table'] == 'master4'
        assert static['state'] == 'up'
        assert static['since'] == '2026-09-25'
        assert static['imported'] == 7818
        assert static['exported'] == 0

    def test_bgp_down_session_fields(self):
        records = ConfigManager._parse_protocols_output(PROTOCOLS_OUTPUT)
        peer = next(r for r in records if r['name'] == 'main_mkt')
        assert peer['state'] == 'start'
        assert peer['bgp_state'] == 'Active'
        assert peer['channel_state'] == 'DOWN'
        assert peer['neighbor'] == '192.168.0.1'
        assert peer['neighbor_as'] == '64999'
        assert peer['local_as'] == '65500'
        assert peer['last_error'] == 'Socket: Connection refused'
        # A down session prints no Routes line, so the counters stay unknown.
        assert peer['imported'] is None
        assert peer['exported'] is None
        assert 'Socket: Connection refused' in peer['info']

    def test_established_session_counters_and_since_with_time(self):
        records = ConfigManager._parse_protocols_output(PROTOCOLS_OUTPUT)
        peer = next(r for r in records if r['name'] == 'peer_lan')
        assert peer['channel_state'] == 'UP'
        assert peer['state'] == 'up'
        assert peer['since'] == '2026-09-26 12:04:11'
        assert peer['imported'] == 1204
        assert peer['exported'] == 1180
        assert peer['last_error'] is None

    def test_idle_session_without_error(self):
        records = ConfigManager._parse_protocols_output(PROTOCOLS_OUTPUT)
        peer = next(r for r in records if r['name'] == 'manual_ru')
        assert peer['channel_state'] == 'DOWN'
        assert peer['state'] == 'idle'
        assert peer['last_error'] is None
        assert peer['imported'] is None

    def test_empty_and_garbage_output(self):
        assert ConfigManager._parse_protocols_output('') == []
        assert ConfigManager._parse_protocols_output(None) == []
        assert ConfigManager._parse_protocols_output('garbage line\n  orphan: 1\n') == []

    def test_bird_protocols_reports_service_and_bgp(
        self, config_manager, monkeypatch, fake_birdc, tmp_path
    ):
        script, _args = fake_birdc
        monkeypatch.setenv('BIRDC_BIN_PATH', str(script))
        monkeypatch.setenv('FAKE_BIRDC_STATUS', PROTOCOLS_OUTPUT)
        socket = tmp_path / 'bird.ctl'
        socket.write_text('')
        monkeypatch.setattr(config_manager, '_bird_socket_path', lambda: str(socket))
        snapshot = config_manager._bird_protocols()
        assert snapshot['error'] is None
        assert snapshot['bird_running'] is True
        assert [p['name'] for p in snapshot['bgp']] == ['main_mkt', 'peer_lan', 'manual_ru']
        assert [s['proto'] for s in snapshot['service']] == ['Static', 'Device', 'Direct']
        assert all(item['in_bird'] is True for item in snapshot['bgp'])

    def test_bird_protocols_when_daemon_is_down(self, config_manager, monkeypatch):
        monkeypatch.setattr(config_manager, '_bird_socket_path', lambda: '/run/bird/missing.ctl')
        snapshot = config_manager._bird_protocols()
        assert snapshot['bird_running'] is False
        assert snapshot['bgp'] == []
        assert snapshot['service'] == []
        assert 'not running' in snapshot['error']
