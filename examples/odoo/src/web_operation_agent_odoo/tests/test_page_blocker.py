"""The application reports error dialogs, not every ordinary action wizard, as blocked."""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ASSET = (Path(__file__).resolve().parents[3] / "deploy" / "addons" / "woa_runtime"
         / "static" / "src" / "page-blocker.js")


@unittest.skipUnless(shutil.which("node"), "Node.js is required for the Odoo blocker contract")
class PageBlockerTests(unittest.TestCase):
    def test_only_visible_error_dialogs_block(self):
        source = ASSET.read_text(encoding="utf-8")
        script = (source.replace("export function readPageBlocker()", "function readPageBlocker()")
                  + "\n"
                  + "const visible = {isConnected:true,getClientRects:()=>[{}]};\n"
                  + "let selector = ''; let rows = [];\n"
                  + "globalThis.document = {querySelectorAll:q=>{selector=q;return rows}};\n"
                  + "rows=[];if(readPageBlocker())process.exit(1);\n"
                  + "rows=[visible];if(!readPageBlocker())process.exit(2);\n"
                  + "if(selector!=='.o_technical_modal .o_error_dialog')process.exit(3);\n"
                  + "rows=[{isConnected:true,getClientRects:()=>[]}];"
                  + "if(readPageBlocker())process.exit(4);\n")
        completed = subprocess.run(["node", "--input-type=module", "-e", script],
                                   capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
