"""
templating
(formerly part of web.py)

Template design:

Template string is split into tokens and the tokens are combined into nodes.
Parse tree is a nodelist. TextNode and ExpressionNode are simple nodes and
for-loop, if-loop etc are block nodes, which contain multiple child nodes.

Grammar:

    template -> defwith sections
    defwith -> '$def with (' arguments ')' | ''
    sections -> section*
    section -> block | assignment | line

    assignment -> '$ ' <assignment expression>
    line -> (text|expr)*
    text -> <any characters other than $>
    expr -> '$' pyexpr | '$(' pyexpr ')' | '${' pyexpr '}'
    pyexpr -> <python expression>
"""

import ast
import builtins as stdlib_builtins
import re
import token
import tokenize
from dataclasses import dataclass
from io import StringIO

__all__ = [
    "Template",
    "ParseError",
]

_LINE_PREFIX_RE = re.compile(r"(?P<indent> *)(?:\$(?P<special>#|\s+|(?:for|while|if|elif|else|def|code|pass|break|continue|return)\b))?")

# `$` used intentionally over `\z`
_HEADER_EOL_RE = re.compile(r"\s*(?:$|\$#)")
_CODE_HEADER_EOL_RE = re.compile(r"\s*(?:$|#)")

_NONSPACE = re.compile(r"\S")


def _tokenize(line):
    return tokenize.generate_tokens(iter((line,)).__next__)


class Node:

    __slots__ = ()

    def emit_chained(self, else_chainable):
        return self.emit()


class BlockNode(Node):

    __slots__ = ("children",)

    def __init__(self):
        self.children = []

    def emit_children(self):
        else_chainable = None

        for child in self.children:
            else_chainable = yield from child.emit_chained(else_chainable=else_chainable)

    def append_text(self, text):
        if not text:
            return

        children = self.children
        if children and isinstance(children[-1], TextNode):
            children[-1].value_parts.append(text)
        else:
            children.append(TextNode(text))


@dataclass(eq=False, frozen=True, slots=True)
class _ElseChainable:

    indent: int | None
    node: ast.AST


class _empty_generator:

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __iter__(self):
        return self

    def __next__(self):
        raise StopIteration(self.value)


class PythonBlockNode(BlockNode):

    __slots__ = (
        "block_type",
        "header",
        "linen0",
        "indent",
    )

    def __init__(self, *, block_type, header, linen0, indent):
        super().__init__()
        self.block_type = block_type
        self.header = header
        self.linen0 = linen0
        self.indent = indent

    def emit_chained(self, else_chainable):
        if self.block_type not in ["elif", "else"]:
            return super().emit_chained(else_chainable)

        if else_chainable is None or not hasattr(else_chainable.node, "orelse"):
            raise ParseError("Unexpected elif/else")

        if self.indent != else_chainable.indent:
            raise ParseError("elif/else indent mismatch")

        is_elif = self.block_type == "elif"
        if is_elif and not isinstance(else_chainable.node, ast.If):
            raise ParseError("Unexpected elif")

        node = ast.parse(f"if ...:\n\t...\n{self.header}\n\t...").body[0]
        ast.increment_lineno(node, self.linen0 - 2)

        assert not else_chainable.node.orelse
        else_chainable.node.orelse = node.orelse

        if is_elif:
            [elif_node] = node.orelse
            assert isinstance(elif_node, ast.If)
            body = elif_node.body
        else:
            body = node.orelse

        body[:] = self.emit_children()

        return _empty_generator(
            _ElseChainable(indent=self.indent, node=elif_node)
        ) if is_elif else ()

    def emit_body(self):
        return self.emit_children()

    def emit(self):
        node = ast.parse(f"{self.header}\n\t...").body[0]
        ast.increment_lineno(node, self.linen0)
        node.body[:] = self.emit_body()
        yield node
        return _ElseChainable(indent=self.indent, node=node)

    def __repr__(self):
        return f"<block: {self.header!r} {self.children!r}>"


class DefNode(PythonBlockNode):

    __slots__ = ()

    def emit_body(self):
        yield from ast.parse("__output = []; __append = __output.append").body
        yield from self.emit_children()
        yield ast.parse("return ''.join(__output)").body[0]


def _append_call(expr):
    return ast.Expr(ast.Call(ast.Name("__append"), args=[expr]), lineno=0, col_offset=0)


class CodeNode(Node):

    __slots__ = (
        "template_source",
        "start_offset",
        "end_offset",
        "start_linen0",
        "body",
    )

    @classmethod
    def begin(cls, *, template_source, start_offset, start_linen0):
        self = cls()
        self.template_source = template_source
        self.start_offset = self.end_offset = start_offset
        self.start_linen0 = start_linen0
        return self

    def end(self, *, same_line=False):
        code = self.template_source[self.start_offset:self.end_offset]
        self.template_source = None
        # XXX: guide to multiline string literals in `$code` blocks: don't
        func = ast.parse("def __code():" + (" " if same_line else "\n") + code).body[0]
        ast.increment_lineno(func, self.start_linen0 - 1)
        self.body = func.body

    @classmethod
    def with_body(cls, body):
        self = cls()
        self.body = body
        return self

    def emit(self):
        return self.body


@dataclass(eq=False, frozen=True, slots=True)
class _StackEntry:

    indent: int
    """The number of spaces of indent."""

    is_code: bool
    """Whether the node is a `CodeNode`, i.e. contains Python instead of template syntax."""

    node: BlockNode | CodeNode


def parse(template_source, *, filename):
    root = DefNode(block_type="def", header="def __template__():", linen0=-1, indent=-1)
    stack = [_StackEntry(indent=-1, is_code=False, node=root)]
    offset = 0

    for linen0, line in enumerate(StringIO(template_source)):
        offset += len(line)

        if linen0 == 0 and (m := re.match(r"\$def with\s*\(", line)) is not None:
            assert line.endswith("\n")
            signature = line[m.end() - 1 : -1]
            root.header = f"def __template__{signature}:"
            root.linen0 = 0
            continue

        assert line
        if line.isspace():  # blank lines are always inside the current block (even if they contain spaces that wouldn't form a sufficient indent)
            top = stack[-1]
            special = None
        else:
            line_prefix = _LINE_PREFIX_RE.match(line)
            indent = line_prefix.end("indent")
            special = line_prefix.group("special")
            while (top := stack[-1]).indent >= indent:
                del stack[-1]
                if top.is_code:
                    top.node.end()

        if top.is_code:
            top.node.end_offset = offset
            continue

        same_line_compound = False
        top_node = top.node
        del top

        # XXX: quadratic time worst case; beware of single line with many compound statements
        while True:
            match special:
                case None:  # normal text with interpolations/comment
                    # trailing backslash suppresses newline
                    if line.endswith("\\\n"):
                        line = line[:-2]

                    # XXX: quadratic time worst case; beware of single line with many interpolations
                    start = 0
                    while (i := line.find("$", start)) != -1:
                        top_node.append_text(line[start:i])
                        match line[i + 1 : i + 2]:
                            case "$":  # escaped `$`
                                top_node.append_text("$")
                                start = i + 2
                            case "#":  # comment
                                if line.endswith("\n"):
                                    top_node.append_text("\n")
                                break
                            case "\n" | "":
                                raise ParseError("Expected '$', '#', or expression after '$'")
                            case ahead:  # interpolation
                                unescaped = ahead == ":"
                                i += 2 if unescaped else 1
                                start = i + _span_expr(line[i:])
                                top_node.children.append(ExpressionNode(line[i : start], escape=not unescaped, linen0=linen0))
                    else:
                        top_node.append_text(line[start:])

                case "code" | "for" | "while" | "if" | "elif" | "else" | "def" as block_type:
                    is_code = block_type == "code"
                    if is_code:
                        header_end = line_prefix.end("special") + 1

                        if line[header_end - 1 : header_end] != ":":
                            raise ParseError("Expected ':' after '$code'")

                        node = CodeNode.begin(template_source=template_source, start_offset=offset, start_linen0=linen0 + 1)
                        eol_re = _CODE_HEADER_EOL_RE
                    else:
                        line = line[line_prefix.start("special"):]
                        header_end = _span_clause_header(line)
                        node_type = DefNode if block_type == "def" else PythonBlockNode
                        node = node_type(block_type=block_type, header=line[:header_end], linen0=linen0, indent=indent)
                        eol_re = _HEADER_EOL_RE

                    top_node.children.append(node)

                    if eol_re.match(line, header_end) is None:  # block is rest of line...
                        same_line_compound = True
                        rest_start = _NONSPACE.search(line, header_end).start()  # ... without leading spaces

                        if is_code:
                            node.start_offset = rest_start
                            node.end_offset = offset
                            node.end(same_line=True)
                            break

                        line = line[rest_start:]
                        line_prefix = _LINE_PREFIX_RE.match(line)
                        indent = None
                        special = line_prefix.group("special")
                        top_node = node
                        continue

                    # next line starts a block
                    if same_line_compound:
                        raise ParseError("Multiple compound statements on same line must finish on same line")

                    stack.append(_StackEntry(indent=indent, is_code=is_code, node=node))

                case "#":  # whole-line comment, doesn't produce blank line or interrupt `elif`/`else`
                    pass

                case _:
                    # A statement line. Original documentation suggests /\$\s+/ was intended for assignment and the other keywords intended to stand alone, but any of them introduces a line that can contain any statement(s).
                    statements = ast.parse(line[line_prefix.start("special"):].lstrip(), filename=filename)
                    ast.increment_lineno(statements, linen0)
                    top_node.children.append(CodeNode.with_body(statements.body))

            break

    top = stack[-1]
    if top.is_code:
        top.node.end()

    return root


_PARENS = {
    token.LPAR: token.RPAR,
    token.LSQB: token.RSQB,
    token.LBRACE: token.RBRACE,
}


def _span_clause_header(text):
    r"""Reads Python tokens up to and including a colon.

    >>> t = 'for i in range(10): hello $name'
    >>> t[:_span_clause_header(t)]
    'for i in range(10):'
    """
    open = []

    for t in _tokenize(text):
        et = t.exact_type

        match t.exact_type:
            case _ if et in _PARENS:
                open.append(_PARENS[et])
            case _ if open:
                if et == open[-1]:
                    del open[-1]
            case token.COLON:
                row, col = t.end
                assert row == 1
                return col
            case token.NEWLINE:
                break

    raise ParseError("Expected clause header to end with a colon")


def _span_expr(text):
    """Reads a Python expression from the text.

    expr -> simple_expr | paren_expr
    simple_expr -> id extended_expr
    extended_expr -> attr_access | paren_expr extended_expr | ''
    attr_access -> dot id extended_expr
    paren_expr -> [ tokens ] | ( tokens ) | { tokens }

        >>> def read_expr(text): return text[:_span_expr(text)]
        ...
        >>> read_expr("name")
        'name'
        >>> read_expr("a.b and c")
        'a.b'
        >>> read_expr("a. b")
        'a'
        >>> read_expr("name</h1>")
        'name'
        >>> read_expr("(limit)ing")
        '(limit)'
        >>> read_expr("(limit)(ing)")
        '(limit)'
        >>> read_expr("f(limit)(ing).foo(bar) .baz")
        'f(limit)(ing).foo(bar)'
        >>> read_expr("limit(ing)")
        'limit(ing)'
        >>> read_expr("limit (ing)")
        'limit'
        >>> read_expr("limit{ing}")
        'limit{ing}'
        >>> read_expr('a[1, 2][:3].f(1+2, "weird string[).", 3 + 4) done.')
        'a[1, 2][:3].f(1+2, "weird string[).", 3 + 4)'
    """

    def extended_expr(last):
        try:
            t = next(tokens)
        except (StopIteration, tokenize.TokenError):
            return last

        if t.start != last.end:
            # space
            return last

        if t.string == ".":
            return attr_access(last, t.end)

        if t.exact_type in _PARENS:
            last = paren_expr(t.exact_type)
            return extended_expr(last)

        return last

    def attr_access(last, dot_end):
        try:
            t = next(tokens)
        except (StopIteration, tokenize.TokenError):
            pass
        else:
            if t.type == token.NAME and t.start == dot_end:
                return extended_expr(t)

        return last

    def paren_expr(begin):
        end = _PARENS[begin]
        while True:
            t = next(tokens, None)
            if t.exact_type == end:
                return t
            if t.exact_type in _PARENS:
                paren_expr(t.exact_type)

    tokens = _tokenize(text)
    t = next(tokens, None)

    # get the last token of the expression
    if t.exact_type == token.LSQB:
        raise ParseError("Unsupported `$[]` interpolation expression; use `$()` or `${}`")
    elif t.exact_type in _PARENS:
        last = paren_expr(t.exact_type)
    elif t.type == token.NAME:
        last = extended_expr(t)  # inlined `simple_expr`
    else:
        # - f-strings tokenize differently across Python versions, so are hard to support
        # - other useful initial tokens conflict with parens
        # - obscure or useless initial tokens are probably mistakes
        raise ParseError(f"Expected unbracketed interpolation expression to start with an identifier: {text!r}")

    row, col = last.end
    assert row == 1
    return col


class TextNode(Node):

    __slots__ = ("value_parts",)

    def __init__(self, value):
        self.value_parts = [value]

    @property
    def value(self):
        return "".join(self.value_parts)

    def emit(self):
        return (_append_call(ast.Constant(self.value)),)

    def __repr__(self):
        return f"t{self.value!r}"


class ExpressionNode(Node):

    __slots__ = ("value", "escape", "linen0")

    def __init__(self, value, *, escape, linen0):
        # ${...}
        if value.startswith("{"):
            assert value.endswith("}")
            value = value[1:-1]

        self.value = ast.parse(value, mode="eval").body
        self.escape = escape
        self.linen0 = linen0

    def emit(self):
        node = _append_call(ast.Call(ast.Name("escape_"), args=[self.value, ast.Constant(self.escape)]))
        ast.increment_lineno(node, self.linen0)
        return (node,)

    def __repr__(self):
        escape = "" if self.escape else ":"
        return f"${escape}({ast.unparse(self.value)})"


def _normalize_text(text):
    r"""Normalize newlines to \n, ensure the text ends with \n, and expand tabs."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs()
    if not text.endswith("\n"):
        text += "\n"

    return text


def _generate_code(text, *, filename):
    # parse the text
    rootnode = parse(text, filename=filename)

    # generate python code from the parse tree
    module = ast.Module(body=list(rootnode.emit()))
    ast.fix_missing_locations(module)
    return module


def _get_source_line(template_string, lineno):
    lines = template_string.splitlines()
    return lines[lineno - 1] if lineno is not None and 1 <= lineno <= len(lines) else None


def _compile_template(template_string, filename):
    code = _generate_code(template_string, filename=filename)

    try:
        # compile the code first to report the errors, if any, with the filename
        compiled_code = compile(code, filename, "exec")
    except SyntaxError as err:
        # display template line that caused the error along with the traceback.
        err.msg += f"\n\nTemplate traceback:\n    File {err.filename!r}, line {err.lineno}\n        {_get_source_line(template_string, err.lineno)}"

        raise

    return compiled_code


class Template:

    def __init__(
        self,
        text,
        *,
        filename="<template>",
        filter,
        globals=None,
        builtins=None,
    ):
        if filter is None:
            raise ValueError("template escaping filter must be specified (e.g. `html.escape`)")
        if globals is None:
            globals = {}
        if builtins is None:
            builtins = stdlib_builtins

        self.filename = filename
        self.filter = filter
        self._globals = globals
        self._builtins = builtins

        text = _normalize_text(text)
        code = _compile_template(text, filename=filename)
        self.t = self._compile(code)

    def _compile(self, code):
        env = self.make_env(self._globals, self._builtins)
        exec(code, env)
        # __template__ is a global function declared when executing "code"
        return env["__template__"]

    def __call__(self, *a, **kw):
        __hidetraceback__ = True  # noqa: F841
        return self.t(*a, **kw)

    def make_env(self, globals, builtins_):
        return dict(
            globals,
            __builtins__=builtins_,
            escape_=self._escape,
        )

    def _escape(self, value, escape):
        if value is None:
            return ""

        value = str(value)
        if escape:
            value = self.filter(value)
        return value

    def __repr__(self):
        """
        >>> Template(text='Template text', filename='burndown_chart.html', filter=...)
        <Template burndown_chart.html>
        """
        return f"<{self.__class__.__name__} {self.filename}>"


class ParseError(Exception):
    pass
