import json
import re
import unittest

from ps_remover import script


class BuildScriptTests(unittest.TestCase):
    def test_config_is_embedded_once_and_output_is_ascii(self):
        config = {"action": "remove", "input": "C:\\사진\\고양이 \"1\".jpg", "ops": []}
        source = script.build_script(config)
        self.assertTrue(source.isascii())
        self.assertNotIn(script.CONFIG_PLACEHOLDER, source)
        match = re.search(r"var PSR_CONFIG = (\{.*?\});\n", source)
        self.assertIsNotNone(match)
        self.assertEqual(json.loads(match.group(1)), config)

    def test_template_placeholder_present(self):
        template = script.TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertEqual(template.count(script.CONFIG_PLACEHOLDER), 1)
        self.assertTrue(template.rstrip().endswith("psrRun(PSR_CONFIG);"))

    def test_to_ascii_js(self):
        self.assertEqual(script.to_ascii_js('"가"'), '"\\uac00"')
        # Characters outside the BMP become surrogate pairs, as in JavaScript.
        self.assertEqual(script.to_ascii_js("😀"), "\\ud83d\\ude00")
        self.assertEqual(script.to_ascii_js("plain"), "plain")

    def test_non_finite_numbers_are_rejected(self):
        with self.assertRaises(ValueError):
            script.build_script({"expand": float("inf")})


if __name__ == "__main__":
    unittest.main()
