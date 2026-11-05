import unittest

from web.template import ExpressionNode, Parser, Template


class TestItem:
    __test__ = False  # silence collection warning from test framework

    def __init__(self):
        self.id = 12345


class TemplateTest(unittest.TestCase):

    def testAttr(self):
        Template("$code:\n    foo = {'a': 1}.items()")()
        Template("$code:\n    bar = {k:0 for k in [1,2,3]}")()

    def test_overridden(self):
        template = "$print('blah')"
        f = Template(template, globals={"print": lambda x: x})
        assert str(f()) == "blah\n"

    def test_quotes(self):
        template = 'a="$foo" <p>'
        f = Template(template, globals={"foo": "bar"})
        assert str(f()) == 'a="bar" <p>\n'

    def test_accessor(self):
        template = 'a="$foo.id"<p>'
        f = Template(template, globals={"foo": TestItem()})
        assert str(f()) == 'a="12345"<p>\n'

    def test_href(self):
        template = '<a href="/del/$item.id">Delete</a>'
        f = Template(template, globals={"item": TestItem()})
        assert str(f()) == '<a href="/del/12345">Delete</a>\n'


class TestParser(unittest.TestCase):
    """
    Test the Parser.

    Tests functions from the Parser class as if the following template were loaded:

    test_template = '''$def with (back, docs)
    $var title: Index
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
