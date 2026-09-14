from __future__ import annotations

import importlib.util
import io
import unittest
from contextlib import redirect_stdout

from bridge_config import PROJECT
from tests.support import configured


def load_renderer():
    spec = importlib.util.spec_from_file_location(
        "render_config", PROJECT / "render-config.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render_config = load_renderer()


class SubstitutionTests(unittest.TestCase):
    def test_every_placeholder_is_replaced(self) -> None:
        result = render_config.render("a=@ONE@ b=@TWO@", {"ONE": "1", "TWO": "2"})
        self.assertEqual(result, "a=1 b=2")

    def test_a_placeholder_the_template_needs_but_we_cannot_fill_is_an_error(
        self,
    ) -> None:
        with self.assertRaisesRegex(RuntimeError, "unresolved placeholder.*MISSING"):
            render_config.render("x=@MISSING@", {"ONE": "1"})

    def test_a_substituted_value_is_never_substituted_again(self) -> None:
        # Sequential str.replace calls would expand the @TWO@ that arrived
        # inside ONE's value, silently injecting an unrelated setting.
        result = render_config.render(
            "name=@ONE@", {"ONE": "Home @TWO@", "TWO": "Desk"}
        )
        self.assertEqual(result, "name=Home @TWO@")

    def test_a_config_value_containing_a_placeholder_does_not_fail_the_render(
        self,
    ) -> None:
        # The guard inspects the template, so a legal user value such as
        # "Kitchen @HOME@" must not abort startup.
        result = render_config.render("n=@ONE@", {"ONE": "Kitchen @HOME@"})
        self.assertEqual(result, "n=Kitchen @HOME@")

    def test_quotes_and_backslashes_are_escaped_for_owntone(self) -> None:
        self.assertEqual(render_config.owntone_escape('a"b'), 'a\\"b')
        self.assertEqual(render_config.owntone_escape("a\\b"), "a\\\\b")


class TemplateTests(unittest.TestCase):
    def test_the_shipped_template_renders_from_the_shipped_example(self) -> None:
        template = (PROJECT / "owntone.conf.in").read_text(encoding="utf-8")
        with configured():
            rendered = render_config.render(template, render_config.replacements())
        self.assertNotIn("@", rendered.replace("@ ", ""))
        # OwnTone matches address text, so the CIDR must not survive verbatim.
        self.assertIn('trusted_networks = { "127.0.0.1", "192.0.2" }', rendered)
        self.assertNotIn("192.0.2.0/24", rendered)

    def test_the_unauthenticated_mpd_port_is_disabled(self) -> None:
        template = (PROJECT / "owntone.conf.in").read_text(encoding="utf-8")
        with configured():
            rendered = render_config.render(template, render_config.replacements())
        mpd_block = rendered.split("mpd {", 1)[1].split("}", 1)[0]
        self.assertIn("port = 0", mpd_block)
        self.assertNotIn("6600", mpd_block)


class ExportTests(unittest.TestCase):
    def test_values_are_shell_quoted_for_the_helper_scripts(self) -> None:
        with configured(DISPLAYPORT_SINK="alsa_output.test-card"):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                render_config.export_shell()
        lines = dict(line.split("=", 1) for line in buffer.getvalue().splitlines())
        self.assertEqual(lines["BRIDGE_UID"], "1000")
        self.assertEqual(lines["DISPLAYPORT_SINK"], "alsa_output.test-card")

    def test_a_value_with_a_space_survives_shell_evaluation(self) -> None:
        with configured(BRIDGE_RUNTIME_DIR="/run/user/1000"):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                render_config.export_shell()
        self.assertIn("BRIDGE_RUNTIME_DIR=/run/user/1000", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
