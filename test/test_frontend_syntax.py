"""Audit M5 (v22): front-end static checks.

node --check catches syntax errors only — the H1-class bug (using an
undefined variable at runtime) sailed through. These greps catch the
most common hand-slip patterns; keep them cheap and deterministic.
"""
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "public", "app.js")
HTML = os.path.join(ROOT, "public", "index.html")


class FrontendSyntaxTest(unittest.TestCase):
    def test_node_check_passes(self):
        """node --check must pass (skipped when node is unavailable)."""
        if subprocess.run(["node", "--version"], capture_output=True).returncode != 0:
            self.skipTest("node not available")
        r = subprocess.run(["node", "--check", APP], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_bare_undefined_variable_patterns(self):
        """Audit M5/H1: catch 'used but never declared' slips statically.

        Heuristic: a statement using `r.` as an object (not a declaration,
        not a parameter, not `const r =`) inside the two WS onmessage
        handlers — the exact pattern that broke chat history rendering.
        We check the simpler invariant: every `.` access on `r` must appear
        in a file that also contains `const r =` or a `r` parameter.
        """
        src = open(APP, encoding="utf-8").read()
        # Count object accesses vs declarations/params.
        obj_access = len(__import__("re").findall(r"(?<![.\w])r\.[A-Za-z_]", src))
        declarations = len(__import__("re").findall(
            r"(?:const|let|var)\s+r\b|= *r\b|\(\s*r\b", src))
        self.assertGreaterEqual(
            declarations, 1,
            "no `r` declaration found — every `r.` access is a ReferenceError")
        self.assertGreaterEqual(
            obj_access, 1, "sanity: expected at least one r.<field> access")

    def test_no_duplicate_identical_lines_in_sensitive_blocks(self):
        """Cheap regression guard for edit accidents (duplicated lines).

        Only CONSECUTIVE duplicates are flagged — the same loop/selector
        legitimately appears in multiple functions, but two identical
        adjacent lines are almost always an edit accident (we hit one in
        applyViewport during audit v20).
        """
        src = open(APP, encoding="utf-8").read()
        lines = [l.strip() for l in src.splitlines() if l.strip()]
        for a, b in zip(lines, lines[1:]):
            if a == b and len(a) > 40:
                self.fail(f"consecutive duplicated line: {a[:80]}")

    def test_escape_html_is_alias_of_esc(self):
        """Audit L6: the two escaping functions must stay the same."""
        src = open(APP, encoding="utf-8").read()
        self.assertIn("const escapeHtml = esc;", src)

    def test_render_tabs_onclick_uses_defined_variable(self):
        """回归:8381e6f 起 tab.onclick 引用了未定义的 session(forEach
        参数是 s)——每次点击 tab 都抛 ReferenceError,tab 切换静默失效
        (滑动/抽屉导航掩盖了它)。浏览器实测发现并修复。"""
        src = open(APP, encoding="utf-8").read()
        # 限定在 renderTabs 作用域(其他函数里 session 是合法参数)
        start = src.index("function renderTabs")
        nxt = src.index("function ", start + len("function renderTabs"))
        rt = src[start:nxt]
        self.assertNotIn("s.id === session.id", rt)
        self.assertIn("sessions.findIndex((x) => x.id === s.id)", rt)

    def test_all_dom_id_references_exist(self):
        """DOM 契约:app.js 引用的元素 id 必须存在于 index.html,
        或在 app.js 里动态创建(模板字面量)。漏 id 会在运行时拿到 null
        并静默破坏对应 UI 功能。"""
        html = open(HTML, encoding="utf-8").read()
        html_ids = set(re.findall(r'id="([^"]+)"', html))
        src = open(APP, encoding="utf-8").read()
        dynamic = set(re.findall(r'id="([A-Za-z0-9_-]+)"', src))
        dynamic |= set(re.findall(r"\.id\s*=\s*['\"]([A-Za-z0-9_-]+)['\"]", src))
        used = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", src))
        missing = sorted(used - html_ids - dynamic)
        self.assertEqual(missing, [],
                         f"getElementById target(s) missing: {missing}")
        qs = set(re.findall(r"querySelector\('#([A-Za-z0-9_-]+)'\)", src))
        missing_qs = sorted(qs - html_ids - dynamic)
        self.assertEqual(missing_qs, [],
                         f"querySelector('#...') target(s) missing: {missing_qs}")


if __name__ == "__main__":
    unittest.main()
