import unittest
from functools import partial

from web.template import ExpressionNode, Parser, Template


class Item:

    id = 12345


t = partial(Template, filter=lambda x: x)


class TemplateTest(unittest.TestCase):

    def testAttr(self):
        t("$code:\n    foo = {'a': 1}.items()")()
        t("$code:\n    bar = {k:0 for k in [1,2,3]}")()

    def test_overridden(self):
        template = "$print('blah')"
        f = t(template, globals={"print": lambda x: x})
        assert f() == "blah\n"

    def test_quotes(self):
        template = 'a="$foo" <p>'
        f = t(template, globals={"foo": "bar"})
        assert f() == 'a="bar" <p>\n'

    def test_accessor(self):
        template = 'a="$foo.id"<p>'
        f = t(template, globals={"foo": Item()})
        assert f() == 'a="12345"<p>\n'

    def test_href(self):
        template = '<a href="/del/$item.id">Delete</a>'
        f = t(template, globals={"item": Item()})
        assert f() == '<a href="/del/12345">Delete</a>\n'

    def test_compat_escape_in_literal(self):
        template = r"${r'\$foo $$bar'}"
        f = t(template)
        assert f() == "\\$foo $$bar\n"

    def test_with_in_def_parameters(self):
        template = "$def with (with_foo = 'with')\n$with_foo"
        f = t(template)
        assert f() == "with\n"


class TestParser(unittest.TestCase):
    """
    Test the Parser.

    Tests functions from the Parser class as if the following template were loaded:

    test_template = '''$def with (back, docs)
    <p><a href="$back">&larr; Back to Index</a></p>
    <ul>
    $for path, title in docs:
        <li><a href="$path">$title</a></li>
    </ul>
    '''
    """

    def test_read_expr(self) -> None:
        """
        Test Parser.read_expr() with the `text` values it would get from
        `Parser.read_node(), if processing `test_template`.
        """
        got = Parser().read_expr('back">&larr; Back to Index</a></p>\n')
        expression_node = got[0]
        assert isinstance(expression_node, ExpressionNode)
        assert repr(expression_node) == "$back"
        assert got[1] == '">&larr; Back to Index</a></p>\n'

        got = Parser().read_expr('path">$title</a></li>\n')
        expression_node = got[0]
        assert isinstance(expression_node, ExpressionNode)
        assert repr(expression_node) == "$path"
        assert got[1] == '">$title</a></li>\n'

        got = Parser().read_expr("title</a></li>\n")
        expression_node = got[0]
        assert isinstance(expression_node, ExpressionNode)
        assert repr(expression_node) == "$title"
        assert got[1] == "</a></li>\n"
