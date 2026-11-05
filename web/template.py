"""
templating
(formerly part of web.py)

Template design:

Template string is split into tokens and the tokens are combined into nodes.
Parse tree is a nodelist. TextNode and ExpressionNode are simple nodes and
for-loop, if-loop etc are block nodes, which contain multiple child nodes.

Each node can emit some python string. python string emitted by the
root node is executed using python in the given environment.

Enough care is taken to make sure the generated code and the template has line to line match,
so that the error messages can point to exact line number in template. (It doesn't work in some cases still.)

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

import builtins as stdlib_builtins
import re
import token
import tokenize

__all__ = [
    "Template",
    "ParseError",
]

_INDENT_RE = re.compile("  +")
_LINE_START_RE = re.compile("^", re.M)


def splitline(text):
    r"""
    Splits the given text at newline.

        >>> splitline('foo\nbar')
        ('foo\n', 'bar')
        >>> splitline('foo')
        ('foo', '')
        >>> splitline('')
        ('', '')
    """
    index = text.find("\n") + 1
    if index:
        return text[:index], text[index:]
    else:
        return text, ""


def _tokenize(line):
    return tokenize.generate_tokens(iter((line,)).__next__)


class Parser:
    """Parser Base."""

    def __init__(self):
        self.statement_nodes = STATEMENT_NODES
        self.keywords = KEYWORDS

    def parse(self, text, name="<template>"):
        self.text = text
        self.name = name

        defwith, text = self.read_defwith(text)
        suite = self.read_suite(text)
        return DefwithNode(defwith, suite)

    def read_defwith(self, text):
        if text.startswith("$def with"):
            defwith, text = splitline(text)
            defwith = defwith.removeprefix("$def with").strip()
            return defwith, text
        else:
            return None, text

    def read_section(self, text):
        r"""Reads one section from the given text.

        section -> block | assignment | line

            >>> read_section = Parser().read_section
            >>> read_section('foo\nbar\n')
            (<line: [t'foo\n']>, 'bar\n')
            >>> read_section('$ a = b + 1\nfoo\n')
            (<assignment: 'a = b + 1'>, 'foo\n')
        """
        if text.lstrip(" ").startswith("$"):
            index = text.index("$")
            begin_indent, text2 = text[:index], text[index + 1 :]
            ahead = self.python_lookahead(text2)

            if ahead in self.statement_nodes:
                return self.read_block_section(ahead, text2, begin_indent)
            elif ahead in self.keywords:
                return self.read_keyword(text2)
            elif ahead.strip() == "":
                # assignments starts with a space after $
                # ex: $ a = b + 2
                return self.read_assignment(text2)
        return self.readline(text)

    def read_suite(self, text):
        r"""Reads section by section till end of text.

        >>> read_suite = Parser().read_suite
        >>> read_suite('hello $name\nfoo\n')
        [<line: [t'hello ', $name, t'\n']>, <line: [t'foo\n']>]
        """
        sections = []
        while text:
            section, text = self.read_section(text)
            sections.append(section)
        return SuiteNode(sections)

    def readline(self, text):
        r"""Reads one line from the text. Newline is suppressed if the line ends with \.

        >>> readline = Parser().readline
        >>> readline('hello $name!\nbye!')
        (<line: [t'hello ', $name, t'!\n']>, 'bye!')
        >>> readline('hello $name!\\\nbye!')
        (<line: [t'hello ', $name, t'!']>, 'bye!')
        >>> readline('$f()\n\n')
        (<line: [$f(), t'\n']>, '\n')
        """
        line, text = splitline(text)

        # suppress new line if line ends with \
        if line.endswith("\\\n"):
            line = line[:-2]

        nodes = []
        while line:
            node, line = self.read_node(line)
            nodes.append(node)

        return LineNode(nodes), text

    def read_node(self, text):
        r"""Reads a node from the given text and returns the node and remaining text.

        >>> read_node = Parser().read_node
        >>> read_node('hello $name')
        (t'hello ', '$name')
        >>> read_node('$name')
        ($name, '')
        """
        if text.startswith("$$"):
            return TextNode("$"), text[2:]
        elif text.startswith("$#"):  # comment
            line, text = splitline(text)
            return TextNode("\n"), text
        elif text.startswith("$"):
            text = text[1:]  # strip $
            if text.startswith(":"):
                escape = False
                text = text[1:]  # strip :
            else:
                escape = True
            return self.read_expr(text, escape=escape)
        else:
            return self.read_text(text)

    def read_text(self, text):
        r"""Reads a text node from the given text.

        >>> read_text = Parser().read_text
        >>> read_text('hello $name')
        (t'hello ', '$name')
        """
        index = text.find("$")
        if index < 0:
            return TextNode(text), ""
        else:
            return TextNode(text[:index]), text[index:]

    def read_keyword(self, text):
        line, text = splitline(text)
        return StatementNode(line.strip() + "\n"), text

    def read_expr(self, text, escape=True):
        """Reads a python expression from the text and returns the expression and remaining text.

        expr -> simple_expr | paren_expr
        simple_expr -> id extended_expr
        extended_expr -> attr_access | paren_expr extended_expr | ''
        attr_access -> dot id extended_expr
        paren_expr -> [ tokens ] | ( tokens ) | { tokens }

            >>> read_expr = Parser().read_expr
            >>> read_expr("name")
            ($name, '')
            >>> read_expr("a.b and c")
            ($a.b, ' and c')
            >>> read_expr("a. b")
            ($a, '. b')
            >>> read_expr("name</h1>")
            ($name, '</h1>')
            >>> read_expr("(limit)ing")
            ($(limit), 'ing')
            >>> read_expr("(limit)(ing)")
            ($(limit), '(ing)')
            >>> read_expr("f(limit)(ing).foo(bar) .baz")
            ($f(limit)(ing).foo(bar), ' .baz')
            >>> read_expr("limit(ing)")
            ($limit(ing), '')
            >>> read_expr("limit (ing)")
            ($limit, ' (ing)')
            >>> read_expr("limit{ing}")
            ($limit{ing}, '')
            >>> read_expr('a[1, 2][:3].f(1+2, "weird string[).", 3 + 4) done.')
            ($a[1, 2][:3].f(1+2, "weird string[).", 3 + 4), ' done.')
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

            if t.string in parens:
                last = paren_expr(t.string)
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
            end = parens[begin]
            while True:
                t = next(tokens)
                if t.string == end:
                    return t
                if t.string in parens:
                    paren_expr(t.string)

        parens = {"(": ")", "[": "]", "{": "}"}

        tokens = _tokenize(text)
        t = next(tokens)

        # get the last token of the expression
        if t.string in parens:
            last = paren_expr(t.string)
        elif t.type == token.NAME:
            last = extended_expr(t)  # inlined `simple_expr`
        else:
            # - f-strings tokenize differently across Python versions, so are hard to support
            # - other useful initial tokens conflict with parens
            # - obscure or useless initial tokens are probably mistakes
            raise ParseError(f"Expected unbracketed interpolation expression to start with an identifier: {text!r}")

        row, col = last.end
        assert row == 1
        return ExpressionNode(text[:col], escape=escape), text[col:]

    def read_assignment(self, text):
        r"""Reads assignment statement from text.

        >>> read_assignment = Parser().read_assignment
        >>> read_assignment('a = b + 1\nfoo')
        (<assignment: 'a = b + 1'>, 'foo')
        """
        line, text = splitline(text)
        return AssignmentNode(line.strip()), text

    def python_lookahead(self, text):
        """Returns the first python token from the given text.

        >>> python_lookahead = Parser().python_lookahead
        >>> python_lookahead('for i in range(10):')
        'for'
        >>> python_lookahead('else:')
        'else'
        >>> python_lookahead(' x = 1')
        ' '
        """
        tokens = _tokenize(text)
        return next(tokens)[1]

    def read_indented_block(self, text, indent):
        r"""Read a block of text. A block is what typically follows a for or if statement.
        It can be in the same line as that of the statement or an indented block.

            >>> read_indented_block = Parser().read_indented_block
            >>> read_indented_block('  a\n  b\nc', '  ')
            ('a\nb\n', 'c')
            >>> read_indented_block('  a\n    b\n  c\nd', '  ')
            ('a\n  b\nc\n', 'd')
            >>> read_indented_block('  a\n\n    b\nc', '  ')
            ('a\n\n  b\n', 'c')
        """
        if indent == "":
            return "", text

        block = ""
        while text:
            line, text2 = splitline(text)
            if line.strip() == "":
                block += "\n"
            elif line.startswith(indent):
                block += line[len(indent) :]
            else:
                break
            text = text2
        return block, text

    def read_statement(self, text):
        r"""Reads a python statement.

        >>> read_statement = Parser().read_statement
        >>> read_statement('for i in range(10): hello $name')
        ('for i in range(10):', ' hello $name')
        """
        open = []

        for t in _tokenize(text):
            et = t.exact_type

            match et:
                case token.LPAR:
                    open.append(token.RPAR)
                case token.LSQB:
                    open.append(token.RSQB)
                case token.LBRACE:
                    open.append(token.RBRACE)
                case _ if open:
                    if et == open[-1]:
                        del open[-1]
                case token.COLON:
                    row, col = t.end
                    assert row == 1
                    return text[:col], text[col:]
                case token.NEWLINE:
                    break

        raise ParseError("Expected clause header to end with a colon")

    def read_block_section(self, keyword, text, begin_indent):
        r"""
            >>> read_block_section = Parser().read_block_section
            >>> read_block_section('for', 'for i in range(10): hello $i\nfoo', begin_indent='')
            (<block: 'for i in range(10):', [<line: [t'hello ', $i, t'\n']>]>, 'foo')
            >>> read_block_section('for', 'for i in range(10):\n        hello $i\n    foo', begin_indent='    ')
            (<block: 'for i in range(10):', [<line: [t'hello ', $i, t'\n']>]>, '    foo')
            >>> read_block_section('for', 'for i in range(10):\n  hello $i\nfoo', begin_indent='')
            (<block: 'for i in range(10):', [<line: [t'hello ', $i, t'\n']>]>, 'foo')

        With inline comment:

            >>> read_block_section('for', 'for i in range(10):  $# inline comment\n hello $i\nfoo', begin_indent='')
            (<block: 'for i in range(10):', []>, ' hello $i\nfoo')
        """
        line, text = splitline(text)
        stmt, line = self.read_statement(line)

        # if there is some thing left in the line
        if line.strip() and not line.lstrip().startswith("$#"):
            block = line.lstrip()
        else:

            def find_indent(text):
                match = _INDENT_RE.match(text)
                first_indent = match and match.group(0)
                return first_indent or ""

            # find the indentation of the block by looking at the first line
            first_indent = find_indent(text)[len(begin_indent) :]

            # TODO: fix this special case
            if keyword == "code":
                indent = begin_indent + first_indent
            else:
                indent = begin_indent + min(first_indent, INDENT)

            block, text = self.read_indented_block(text, indent)

        return self.statement_nodes[keyword](stmt, block, begin_indent), text


class DefwithNode:
    def __init__(self, defwith, suite):
        if defwith is not None:
            self.defwith = f"def __template__{defwith}:"
            # offset 2 lines for _lineoffset_ and self.
            self.defwith += "\n    _lineoffset_ = -2"
        else:
            self.defwith = "def __template__():"
            # offset 3 lines for __template__, _lineoffset_, and self.
            self.defwith += "\n    _lineoffset_ = -3"

        self.defwith += "\n    __output = []; extend_ = __output.extend"
        self.suite = suite
        self.end = "\n    return ''.join(__output)"

    def emit(self, indent):
        return self.defwith + self.suite.emit(indent + INDENT) + self.end

    def __repr__(self):
        return f"<defwith: {self.defwith}, {self.suite}>"


class TextNode:
    def __init__(self, value):
        self.value = value

    def emit(self, indent, begin_indent=""):
        return repr(self.value)

    def __repr__(self):
        return f"t{self.value!r}"


class ExpressionNode:
    def __init__(self, value, escape=True):
        self.value = value.strip()

        # convert ${...} to $(...)
        if value.startswith("{") and value.endswith("}"):
            self.value = "(" + self.value[1:-1] + ")"

        self.escape = escape

    def emit(self, indent, begin_indent=""):
        return f"escape_({self.value}, {bool(self.escape)})"

    def __repr__(self):
        if self.escape:
            escape = ""
        else:
            escape = ":"
        return f"${escape}{self.value}"


class AssignmentNode:
    def __init__(self, code):
        self.code = code

    def emit(self, indent, begin_indent=""):
        return indent + self.code + "\n"

    def __repr__(self):
        return f"<assignment: {self.code!r}>"


class LineNode:
    def __init__(self, nodes):
        self.nodes = nodes

    def emit(self, indent, text_indent="", name=""):
        text = [node.emit("") for node in self.nodes]
        if text_indent:
            text = [repr(text_indent)] + text

        return indent + "extend_([%s])\n" % ", ".join(text)

    def __repr__(self):
        return f"<line: {self.nodes!r}>"


INDENT = "    "  # 4 spaces


class BlockNode:
    def __init__(self, stmt, block, begin_indent=""):
        self.stmt = stmt
        self.suite = Parser().read_suite(block)
        self.begin_indent = begin_indent

    def emit(self, indent, text_indent=""):
        text_indent = self.begin_indent + text_indent
        out = indent + self.stmt + self.suite.emit(indent + INDENT, text_indent)
        return out

    def __repr__(self):
        return f"<block: {self.stmt!r}, {self.suite!r}>"


class CodeNode:
    def __init__(self, stmt, block, begin_indent=""):
        # compensate one line for $code:
        self.code = "\n" + block

    def emit(self, indent, text_indent=""):
        return _LINE_START_RE.sub(indent, self.code).rstrip(" ")

    def __repr__(self):
        return f"<code: {self.code!r}>"


class StatementNode:
    def __init__(self, stmt):
        self.stmt = stmt

    def emit(self, indent, begin_indent=""):
        return indent + self.stmt

    def __repr__(self):
        return f"<stmt: {self.stmt!r}>"


class IfNode(BlockNode):
    pass


class ElseNode(BlockNode):
    pass


class ElifNode(BlockNode):
    pass


class DefNode(BlockNode):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)

        code = CodeNode("", "")
        code.code = "__output = []; extend_ = __output.extend\n"
        self.suite.sections.insert(0, code)

        code = CodeNode("", "")
        code.code = "return ''.join(__output)\n"
        self.suite.sections.append(code)

    def emit(self, indent, text_indent=""):
        text_indent = self.begin_indent + text_indent
        out = indent + self.stmt + self.suite.emit(indent + INDENT, text_indent)
        return indent + "_lineoffset_ -= 3\n" + out


class SuiteNode:
    """Suite is a list of sections."""

    def __init__(self, sections):
        self.sections = sections

    def emit(self, indent, text_indent=""):
        return "\n" + "".join([s.emit(indent, text_indent) for s in self.sections])

    def __repr__(self):
        return repr(self.sections)


STATEMENT_NODES = {
    "for": BlockNode,
    "while": BlockNode,
    "if": IfNode,
    "elif": ElifNode,
    "else": ElseNode,
    "def": DefNode,
    "code": CodeNode,
}

KEYWORDS = ["pass", "break", "continue", "return"]


class BaseTemplate:
    def __init__(self, code, filename, filter, globals, builtins):
        self.filename = filename
        self.filter = filter
        self._globals = globals
        self._builtins = builtins
        if code:
            self.t = self._compile(code)
        else:
            self.t = lambda: ""

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

    def _escape(self, value, escape=False):
        if value is None:
            return ""

        value = str(value)
        if escape:
            value = self.filter(value)
        return value


class Template(BaseTemplate):

    def __init__(
        self,
        text,
        *,
        filename="<template>",
        filter,
        globals=None,
        builtins=None,
    ):
        text = Template.normalize_text(text)
        code = self.compile_template(text, filename)

        if filter is None:
            raise ValueError("template escaping filter must be specified (e.g. `html.escape`)")
        if globals is None:
            globals = {}
        if builtins is None:
            builtins = stdlib_builtins

        super().__init__(
            code=code,
            filename=filename,
            filter=filter,
            globals=globals,
            builtins=builtins,
        )

    def __repr__(self):
        """
        >>> Template(text='Template text', filename='burndown_chart.html', filter=...)
        <Template burndown_chart.html>
        """
        return f"<{self.__class__.__name__} {self.filename}>"

    @staticmethod
    def normalize_text(text):
        r"""Normalize newlines to \n, ensure the text ends with \n, and expand tabs."""
        text = text.replace("\r\n", "\n").replace("\r", "\n").expandtabs()
        if not text.endswith("\n"):
            text += "\n"

        return text

    @staticmethod
    def _generate_code(text, filename):
        # parse the text
        rootnode = Parser().parse(text, filename)

        # generate python code from the parse tree
        return rootnode.emit(indent="").strip()

    def compile_template(self, template_string, filename):
        code = Template._generate_code(template_string, filename)

        def get_source_line(lineno):
            lines = template_string.splitlines()
            return lines[lineno - 1] if lineno is not None and 1 <= lineno <= len(lines) else None

        try:
            # compile the code first to report the errors, if any, with the filename
            compiled_code = compile(code, filename, "exec")
        except SyntaxError as err:
            # display template line that caused the error along with the traceback.
            err.msg += f"\n\nTemplate traceback:\n    File {err.filename!r}, line {err.lineno}\n        {get_source_line(err.lineno)}"

            raise

        return compiled_code


class ParseError(Exception):
    pass
