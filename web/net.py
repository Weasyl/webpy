def htmlquote(text):
    r"""
    Encodes `text` for raw use in HTML.

        >>> htmlquote(u"<'&\">")
        '&lt;&#39;&amp;&quot;&gt;'
    """
    text = text.replace("&", "&amp;")  # Must be done first!
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace("'", "&#39;")
    text = text.replace('"', "&quot;")
    return text


def websafe(val):
    r"""
    Converts `val` so that it is safe for use in Unicode HTML.

        >>> websafe("<'&\">")
        '&lt;&#39;&amp;&quot;&gt;'
        >>> websafe(None)
        ''
        >>> websafe(u'\u203d') == u'\u203d'
        True
    """
    if val is None:
        return ""

    if isinstance(val, bytes):
        val = val.decode("utf-8")
    elif not isinstance(val, str):
        val = str(val)

    return htmlquote(val)
