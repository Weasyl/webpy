import unittest
from functools import partial

from web.template import Template


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
