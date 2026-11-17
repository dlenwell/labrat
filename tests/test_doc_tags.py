# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Hold every function to the tagged-docstring contract (scripts/doc_tags.py)."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import doc_tags  # noqa: E402  (scripts/ is not a package)


class DocTagTests(unittest.TestCase):
    """Python docstrings and JSDoc blocks name what each function takes and gives."""

    def test_every_function_is_documented_with_tags(self) -> None:
        """No module, class, function, or method is missing its doc or tags."""
        problems = doc_tags.all_problems()
        self.assertEqual(problems, [], "Add the missing docs (run scripts/doc_tags.py):\n" + "\n".join(problems))

    def test_checker_reads_python_tags(self) -> None:
        """Args, Returns, and Raises are required only when they apply."""
        import ast
        source = '''
def good(value):
    """Double a value.

    Args:
        value: A number.

    Returns:
        int: Twice it.

    Raises:
        ValueError: When it is negative.
    """
    if value < 0:
        raise ValueError("negative")
    return value * 2

def bare(value):
    """Double a value."""
    return value * 2

def caught():
    """Swallow an error raised inside its own try."""
    try:
        raise ValueError("x")
    except ValueError:
        pass
'''
        functions = {node.name: node for node in ast.parse(source).body}
        relative = Path("example.py")
        self.assertEqual(doc_tags._function_problems(relative, functions["good"]), [])
        self.assertEqual(doc_tags._function_problems(relative, functions["caught"]), [])
        bare = doc_tags._function_problems(relative, functions["bare"])
        self.assertTrue(any("Args:" in problem for problem in bare))
        self.assertTrue(any("Returns:" in problem for problem in bare))

    def test_checker_reads_jsdoc(self) -> None:
        """Nested types, wrapped names, and nested callbacks are read correctly."""
        doc = "/**\n * @param {{a: Array<{b: number}>}}\n *     spec The spec.\n * @param {number} [count=1] - How many.\n */"
        self.assertEqual(doc_tags._jsdoc_param_names(doc), ["spec", "count"])
        body = "function f(items) {\n    items.forEach((item) => { return item; });\n    // the card's lamp\n    run();\n}"
        self.assertFalse(doc_tags._returns_value(body))
        self.assertTrue(doc_tags._returns_value("function g() {\n    return 1;\n}"))
        wrapped = ["function wrapped(", "    alpha, beta,", ") {", "    return alpha;", "}"]
        self.assertTrue(doc_tags._joined_signature(wrapped, 0).startswith("function wrapped( alpha, beta, ) {"))


if __name__ == "__main__":
    unittest.main()
