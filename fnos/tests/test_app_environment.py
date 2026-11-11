import unittest
from fnos.integration.image_assembly.app_environment import codec_mask, encode, migration


class ApplicationEnvironmentTests(unittest.TestCase):
    def test_native_codec_known_non_secret_value(self):
        self.assertEqual(encode("auto", b"12345678901234567890"), "UEdHWw")

    def test_unreviewed_vendor_binary_rejected(self):
        with self.assertRaises(ValueError):
            codec_mask(b"unreviewed vendor executable")

    def test_library_path_cannot_inject_sql(self):
        for path in ("/tmp/other.so", "/usr/lib/fnos-x5/libhome_acl-'bad'.so"):
            with self.assertRaises(ValueError):
                migration(path, b"12345678901234567890")


if __name__ == "__main__":
    unittest.main()
