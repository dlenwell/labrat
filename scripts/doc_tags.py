# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Check that LabRat's functions carry tagged docstrings and JSDoc.

The readability contract (CONTRIBUTING.md, "Conventions"): every
Python module, class, and function has a PEP 257 docstring in Google style,
with ``Args:`` naming each parameter, ``Returns:`` when it returns a value,
``Yields:`` when it yields, and ``Raises:`` when it raises on purpose. Every
JavaScript function declaration and class method has a JSDoc block with an
``@param`` per parameter and ``@returns`` when it returns a value.

Closures defined inside another function (Python nested functions, JS
callbacks and ``const`` helpers inside a function body) are exempt: their
enclosing function documents them. In ``tests/``, ``test_*`` methods and
unittest's hooks are exempt too; test modules, classes, and helpers are not.

Run it to list what is missing::

    .venv/bin/python scripts/doc_tags.py

``tests/test_doc_tags.py`` runs the same checks in the gate.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
PYTHON_DIRS = ("src/labrat", "scripts", "tests", "migrations")
#: Test methods are named for what they check, and unittest calls its hooks
#: itself, so these need no docstring tags; their classes and helpers do.
EXEMPT = re.compile(r"^(test_\w*|setUp|tearDown|setUpClass|tearDownClass|setUpModule|tearDownModule)$")
JS_DIR = "src/labrat/static/js"

_JS_FUNCTION = re.compile(r"^(?P<indent>[ \t]*)(?:async\s+)?function\s*\*?\s*(?P<name>\w+)\s*\((?P<params>[^)]*)\)\s*\{")
_JS_METHOD = re.compile(
    r"^(?P<indent>[ \t]+)(?:static\s+)?(?:async\s+)?(?:get\s+|set\s+)?(?P<name>(?!if\b|for\b|while\b|switch\b|catch\b|function\b|return\b)[A-Za-z_$][\w$]*)"
    r"\s*\((?P<params>[^)]*)\)\s*\{\s*$"
)
_JS_CLASS = re.compile(r"^(?P<indent>[ \t]*)class\s+\w+")


def _python_raises(function: ast.AST) -> bool:
    """Tell whether a function raises on purpose outside a ``try`` body.

    A ``raise`` inside a ``try`` block is usually caught by its own handler,
    and a bare ``raise`` re-raises what a handler caught, so neither counts.

    Args:
        function: A function definition node.

    Returns:
        bool: True when the function body raises an exception itself.
    """
    def visit(node: ast.AST, in_try: bool) -> bool:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Raise) and child.exc is not None and not in_try:
                return True
            if isinstance(child, ast.Try):
                if any(visit(statement, True) for statement in child.body):
                    return True
                for part in (child.handlers, child.orelse, child.finalbody):
                    for statement in part:
                        if isinstance(statement, ast.Raise) and statement.exc is not None and not in_try:
                            return True
                        if visit(statement, in_try):
                            return True
                continue
            if visit(child, in_try):
                return True
        return False

    return visit(function, False)


def _python_returns(function: ast.AST) -> tuple[bool, bool]:
    """Tell whether a function returns a value or yields, ignoring closures.

    Args:
        function: A function definition node.

    Returns:
        tuple[bool, bool]: Whether it returns a value, and whether it yields.
    """
    returns = yields = False
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Return) and node.value is not None and not (
            isinstance(node.value, ast.Constant) and node.value.value is None
        ):
            returns = True
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            yields = True
        stack.extend(ast.iter_child_nodes(node))
    return returns, yields


def python_problems(path: Path) -> list[str]:
    """List the missing docstrings and tags in one Python file.

    Args:
        path: The Python source file.

    Returns:
        list[str]: One ``path:line name: problem`` entry per gap.
    """
    relative = path.relative_to(ROOT)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems = []
    if not ast.get_docstring(tree):
        problems.append(f"{relative}:1 module: no docstring")

    def check(node: ast.AST, nested: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                if not ast.get_docstring(child) and not nested:
                    problems.append(f"{relative}:{child.lineno} {child.name}: no docstring")
                check(child, nested)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not nested and not (relative.parts[0] == "tests" and EXEMPT.match(child.name)):
                    problems.extend(_function_problems(relative, child))
                check(child, True)
            else:
                check(child, nested)

    check(tree, False)
    return problems


def _function_problems(relative: Path, function: ast.FunctionDef) -> list[str]:
    """List one function's missing docstring or tags.

    Args:
        relative: The file's path from the repository root, for messages.
        function: The function definition node.

    Returns:
        list[str]: The function's gaps, empty when its docstring is complete.
    """
    where = f"{relative}:{function.lineno} {function.name}"
    doc = ast.get_docstring(function)
    if not doc:
        return [f"{where}: no docstring"]
    problems = []
    arguments = function.args
    names = [a.arg for a in arguments.posonlyargs + arguments.args + arguments.kwonlyargs if a.arg not in ("self", "cls")]
    if arguments.vararg:
        names.append(arguments.vararg.arg)
    if arguments.kwarg:
        names.append(arguments.kwarg.arg)
    if names:
        if "Args:" not in doc:
            problems.append(f"{where}: no Args: section")
        else:
            section = doc.split("Args:", 1)[1]
            missing = [name for name in names if not re.search(rf"^\s*\**{re.escape(name)}\b", section, re.M)]
            if missing:
                problems.append(f"{where}: Args: does not name {', '.join(missing)}")
    returns, yields = _python_returns(function)
    if returns and "Returns:" not in doc:
        problems.append(f"{where}: no Returns: section")
    if yields and "Yields:" not in doc:
        problems.append(f"{where}: no Yields: section")
    if _python_raises(function) and "Raises:" not in doc:
        problems.append(f"{where}: no Raises: section")
    return problems


def _js_params(params: str) -> list[str]:
    """Name a JS parameter list's parameters as JSDoc would.

    A destructured parameter counts as one, documented under any name.

    Args:
        params: The text between a function's parentheses.

    Returns:
        list[str]: One entry per parameter: its name, or "{}" / "[]" for a
            destructured one.
    """
    names, depth, current = [], 0, ""
    for character in params:
        if character in "{[(":
            depth += 1
        elif character in "}])":
            depth -= 1
        if character == "," and depth == 0:
            names.append(current.strip())
            current = ""
        else:
            current += character
    if current.strip():
        names.append(current.strip())
    result = []
    for name in names:
        name = name.lstrip(".")
        if name.startswith("{"):
            result.append("{}")
        elif name.startswith("["):
            result.append("[]")
        else:
            result.append(re.split(r"\s*=", name, maxsplit=1)[0].strip())
    return result


def _js_body(lines: list[str], start: int) -> str:
    """Return a JS function's body by counting braces from its first line.

    Args:
        lines: The file's lines.
        start: Index of the line that opens the function.

    Returns:
        str: The function's text, from its opening line to its closing brace.
    """
    depth, seen, body = 0, False, []
    for line in lines[start:]:
        stripped = re.sub(r"(^|\s)//.*$", r"\1", line)
        stripped = re.sub(r"(['\"`])(?:\\.|(?!\1).)*\1", '""', stripped)
        body.append(line)
        depth += stripped.count("{") - stripped.count("}")
        seen = seen or "{" in stripped
        if seen and depth <= 0:
            break
    return "\n".join(body)


def _strip_nested_functions(body: str) -> str:
    """Remove the bodies of function literals nested inside a JS function.

    Strings, template literals, and comments are blanked first so braces
    inside them do not count.

    Args:
        body: The function's text from ``_js_body``, opening line first.

    Returns:
        str: The text with every nested ``=> { ... }`` and
            ``function (...) { ... }`` body removed.
    """
    # Comments first: an apostrophe in one ("the card's lamp") is not a
    # string. A "//" counts only at a line start or after whitespace, so
    # URLs inside strings survive.
    text = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    text = re.sub(r"(^|\s)//[^\n]*", r"\1", text)
    text = re.sub(r"(['\"`])(?:\\.|(?!\1).)*?\1", '""', text, flags=re.S)
    opening = text.index("{") + 1 if "{" in text else 0
    head, rest = text[:opening], text[opening:]
    nested = re.compile(r"=>\s*\{|\bfunction\b[^{(]*\([^)]*\)\s*\{")
    out, index = [], 0
    while True:
        found = nested.search(rest, index)
        if not found:
            out.append(rest[index:])
            break
        out.append(rest[index:found.start()])
        depth, cursor = 1, found.end()
        while cursor < len(rest) and depth:
            depth += {"{": 1, "}": -1}.get(rest[cursor], 0)
            cursor += 1
        index = cursor
    return head + "".join(out)


def _returns_value(body: str) -> bool:
    """Tell whether a JS function's own body returns a value.

    Returns inside nested arrow functions and callbacks are not counted.

    Args:
        body: The function's text from ``_js_body``.

    Returns:
        bool: True when the function itself returns a value.
    """
    return bool(re.search(r"\breturn\s+[^;\s}]", _strip_nested_functions(body)))


def _jsdoc_param_names(doc: str) -> list[str]:
    """Read the parameter names a JSDoc block documents.

    Types may nest braces (``{{a: Array<{b: number}>}}``), and a long type
    may push the name onto the next line.

    Args:
        doc: The JSDoc block's text.

    Returns:
        list[str]: Each ``@param`` name in order, such as ``options.limit``.
    """
    text = re.sub(r"\n\s*\*\s?", " ", doc)
    names = []
    for found in re.finditer(r"@param\b\s*", text):
        index = found.end()
        if index < len(text) and text[index] == "{":
            depth = 0
            while index < len(text):
                depth += {"{": 1, "}": -1}.get(text[index], 0)
                index += 1
                if depth == 0:
                    break
        name = re.match(r"\s*\[?([\w$.]+)", text[index:])
        if name:
            names.append(name.group(1))
    return names


def _joined_signature(lines: list[str], index: int) -> str:
    """Join a function or method signature wrapped over several lines.

    Args:
        lines: The file's lines.
        index: The line that may start a signature.

    Returns:
        str: The line, with any following lines up to the signature's
            opening brace joined on when its parentheses are still open.
    """
    line = lines[index]
    if not re.match(r"^\s*(?:static\s+)?(?:async\s+)?(?:function\s*\*?\s*)?[A-Za-z_$][\w$]*\s*\($", line.rstrip()[: line.rstrip().rfind("(") + 1]):
        return line
    joined, depth = line, line.count("(") - line.count(")")
    for follow in lines[index + 1:index + 12]:
        if depth <= 0:
            break
        joined += " " + follow.strip()
        depth += follow.count("(") - follow.count(")")
    return joined if re.search(r"\)\s*\{\s*$", joined) else line


def js_problems(path: Path) -> list[str]:
    """List the missing JSDoc blocks and tags in one JavaScript file.

    Args:
        path: The script.

    Returns:
        list[str]: One ``path:line name: problem`` entry per gap.
    """
    relative = path.relative_to(ROOT)
    lines = path.read_text(encoding="utf-8").split("\n")
    problems = []
    class_indents = []
    for index, raw in enumerate(lines):
        line = _joined_signature(lines, index)
        if (found := _JS_CLASS.match(line)):
            class_indents.append(len(found.group("indent")) + 4)
        function = _JS_FUNCTION.match(line)
        method = _JS_METHOD.match(line)
        if function:
            indent = len(function.group("indent"))
            if indent > 4:
                continue
        elif method and len(method.group("indent")) in class_indents:
            function = method
        else:
            continue
        name = function.group("name")
        params = _js_params(function.group("params"))
        where = f"{relative}:{index + 1} {name}"
        end = index - 1
        while end >= 0 and not lines[end].strip():
            end -= 1
        if end < 0 or not lines[end].strip().endswith("*/"):
            problems.append(f"{where}: no JSDoc")
            continue
        start = end
        while start >= 0 and "/**" not in lines[start]:
            start -= 1
        if start < 0:
            problems.append(f"{where}: no JSDoc")
            continue
        doc = "\n".join(lines[start:end + 1])
        documented = _jsdoc_param_names(doc)
        top_level = [name for name in documented if "." not in name]
        if params:
            if len(top_level) < len(params):
                missing = [p for p in params if p not in ("{}", "[]") and p not in top_level]
                unnamed = len(params) - len(top_level)
                problems.append(f"{where}: @param missing for {', '.join(missing) or f'{unnamed} parameter(s)'}")
        if name != "constructor" and "@return" not in doc and _returns_value(_js_body(lines, index)):
            problems.append(f"{where}: no @returns")
    return problems


def all_problems() -> list[str]:
    """Check every Python file in PYTHON_DIRS, and every page script.

    Returns:
        list[str]: Every gap found, Python first.
    """
    problems = []
    for directory in PYTHON_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            problems.extend(python_problems(path))
    for path in sorted((ROOT / JS_DIR).glob("*.js")):
        problems.extend(js_problems(path))
    return problems


if __name__ == "__main__":
    found = all_problems()
    print("\n".join(found))
    print(f"{len(found)} gap(s)", file=sys.stderr)
    sys.exit(1 if found else 0)
