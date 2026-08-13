from __future__ import annotations

import unittest

from storyforge.api import StoryForgeApi
from storyforge.rpc_contract import (
    CATALOG_RPC_METHODS,
    CLIENT_LOCAL_MEDIA_METHODS,
    CONNECTION_IDENTITY_RPC_METHOD,
    DEVICE_CAPABILITY_FIELDS,
    HUB_RPC_PERMISSION_ANY,
    LEGACY_DEVICE_CAPABILITY_FIELDS,
    LOCAL_WORKER_PRIVATE_METHODS,
    LOCAL_WORKER_RPC_PERMISSIONS,
    WEB_DESKTOP_ONLY_MEDIA_METHODS,
    WEB_RPC_PERMISSIONS,
    validate_rpc_contract,
)


class RpcContractTests(unittest.TestCase):
    def test_contract_is_internally_consistent(self) -> None:
        self.assertEqual(validate_rpc_contract(), ())

    def test_web_contract_only_exposes_real_api_methods(self) -> None:
        missing = sorted(
            method
            for method in WEB_RPC_PERMISSIONS
            if not callable(getattr(StoryForgeApi, method, None))
        )
        self.assertEqual(missing, [])

    def test_hub_permission_rules_cover_catalog_surface(self) -> None:
        permission_free = {
            "bootstrap_summary",
            CONNECTION_IDENTITY_RPC_METHOD,
            "get_effective_permissions",
        }
        self.assertEqual(
            CATALOG_RPC_METHODS - permission_free - HUB_RPC_PERMISSION_ANY.keys(),
            set(),
        )

    def test_connection_identity_is_an_explicit_hub_rpc(self) -> None:
        self.assertIn(CONNECTION_IDENTITY_RPC_METHOD, CATALOG_RPC_METHODS)
        self.assertNotIn(CONNECTION_IDENTITY_RPC_METHOD, HUB_RPC_PERMISSION_ANY)

    def test_local_worker_private_methods_are_explicit(self) -> None:
        private = LOCAL_WORKER_RPC_PERMISSIONS.keys() - WEB_RPC_PERMISSIONS.keys()
        self.assertEqual(private, LOCAL_WORKER_PRIVATE_METHODS)

    def test_client_local_methods_remain_inside_desktop_surface(self) -> None:
        self.assertTrue(
            CLIENT_LOCAL_MEDIA_METHODS <= WEB_DESKTOP_ONLY_MEDIA_METHODS
        )

    def test_device_capability_contract_preserves_legacy_projection(self) -> None:
        self.assertEqual(
            LEGACY_DEVICE_CAPABILITY_FIELDS,
            {
                "device_config_sync",
                "local_render",
                "local_tts",
                "local_subtitles",
            },
        )
        self.assertTrue(
            LEGACY_DEVICE_CAPABILITY_FIELDS <= DEVICE_CAPABILITY_FIELDS
        )
        self.assertIn("production_contract", DEVICE_CAPABILITY_FIELDS)
        self.assertNotIn("production_contract", LEGACY_DEVICE_CAPABILITY_FIELDS)


if __name__ == "__main__":
    unittest.main()
