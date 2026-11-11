#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
umask 022
python3 - <<'TESTS'
import unittest,sys
suite=unittest.TestSuite()
for name in ['test_app_environment.py', 'test_face_namespace.py', 'test_home_storage.py', 'test_image_identity.py', 'test_lab.py', 'test_native_media_launcher.py', 'test_native_photos.py', 'test_native_semantic768_staging.py', 'test_official_detector.py', 'test_photos_storage_roots.py', 'test_platform_runtime.py', 'test_product_ai_defaults.py', 'test_product_bootstrap.py', 'test_semantic768_evidence.py', 'test_semantic768_runtime_identity.py', 'test_semantic768_text_controller.py', 'test_semantic_conversion.py']:
 suite.addTests(unittest.defaultTestLoader.discover('fnos/tests',pattern=name))
result=unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(not result.wasSuccessful())
TESTS
