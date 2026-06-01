from __future__ import annotations

import unittest
from unittest.mock import patch

from src import vector_store


class VectorStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        vector_store._CHROMADB_MODULE = None
        vector_store._CHROMADB_IMPORT_ATTEMPTED = False
        vector_store._CHROMADB_UNAVAILABLE_LOGGED = False

    def tearDown(self) -> None:
        vector_store._CHROMADB_MODULE = None
        vector_store._CHROMADB_IMPORT_ATTEMPTED = False
        vector_store._CHROMADB_UNAVAILABLE_LOGGED = False

    def test_missing_chromadb_warns_only_once(self) -> None:
        with patch(
            "src.vector_store._attempt_chromadb_import",
            side_effect=ImportError("No module named 'chromadb'"),
        ), patch("src.vector_store.logger.warning") as warning_mock:
            self.assertFalse(vector_store.chroma_backend_available())
            self.assertFalse(vector_store.chroma_backend_available())

        warning_mock.assert_called_once()

    def test_upsert_target_profile_document_returns_false_when_backend_unavailable(self) -> None:
        with patch("src.vector_store.get_named_collection", return_value=None):
            saved = vector_store.upsert_target_profile_document(
                platform="instagram",
                target_username="k.dvorakovaa",
                source_kind="deep_media_analysis",
                source_id="entry-1",
                content="Visible summary",
                metadata={},
                data_dir=vector_store.Path("/tmp/social-scanner-data"),
            )

        self.assertFalse(saved)


if __name__ == "__main__":
    unittest.main()
