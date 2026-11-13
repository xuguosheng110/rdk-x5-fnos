import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fnos.integration.image_assembly import media_web


class MediaWebTests(unittest.TestCase):
    def test_apply_refresh_and_remove_only_from_reviewed_index(self):
        native = b'<html><head></head><body>native</body></html>'
        player = b'async()=>{' + media_web.ANCHOR + b'})'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            target = root / 'usr/local/apps/@appcenter/trim.media'
            index = target / 'static/index.html'
            asset = index.parent / 'assets' / media_web.PLAYER_ASSET
            asset.parent.mkdir(parents=True)
            index.write_bytes(native)
            asset.write_bytes(player)
            link = root / media_web.APP_TARGET
            link.parent.mkdir(parents=True)
            link.symlink_to(target, target_is_directory=True)
            shim = root / media_web.SHIM
            shim.parent.mkdir(parents=True)
            shim.write_bytes(b'firstVersion();\n')

            with patch.object(media_web, 'INDEX_SHA256', hashlib.sha256(native).hexdigest()), \
                    patch.object(media_web, 'PLAYER_SHA256', hashlib.sha256(player).hexdigest()):
                self.assertEqual(media_web.reconcile(root, enabled=True), 'enabled')
                self.assertIn(b'firstVersion();\n', asset.read_bytes())
                shim.write_bytes(b'newVersion();\n')
                self.assertEqual(media_web.reconcile(root, enabled=True), 'enabled')
                self.assertEqual(asset.read_bytes().count(media_web.PATCH_START), 1)
                self.assertIn(b'newVersion();\n', asset.read_bytes())
                self.assertEqual(media_web.reconcile(root, enabled=False), 'native')
                self.assertEqual(index.read_bytes(), native)
                self.assertEqual(asset.read_bytes(), player)

                index.write_bytes(native.replace(b'<head>', b'<head>' + media_web.START + b'legacy' + media_web.END))
                media_web.reconcile(root, enabled=True)
                asset.write_bytes(b'updated player')
                self.assertEqual(media_web.reconcile(root, enabled=True), 'unknown_player')
                self.assertEqual(index.read_bytes(), native)
                self.assertEqual(asset.read_bytes(), b'updated player')

                updated_index = native + b'updated index'
                index.write_bytes(updated_index)
                self.assertEqual(media_web.reconcile(root, enabled=True), 'unknown_index')
                self.assertEqual(index.read_bytes(), updated_index)
