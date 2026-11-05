from html import escape as html_escape


def websafe(val):
    r"""
    Converts `val` so that it is safe for use in Unicode HTML.

        >>> websafe("<'&\">")
        '&lt;&#x27;&amp;&quot;&gt;'
        >>> websafe(None)
        ''
        >>> websafe('\u203d')
        '\u203d'
    """
    if val is None:
        return ""

    if isinstance(val, bytes):
        val = val.decode("utf-8")
    elif not isinstance(val, str):
        val = str(val)

    return html_escape(val)
