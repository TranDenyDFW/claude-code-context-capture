"""Command-line access to everything the dashboard renders.

The app draws its numbers into a browser, so for most of this project's life the only way to ask
whether a figure was right was to look at it and ask someone. These modules run the same callbacks
and hand back plain data, which is what lets the tests in tests/ assert on a tab without a browser.

  extract   component tree  ->  tables, figures, prose
  render    that data       ->  text or JSON for a terminal
  commands  one function per subcommand
  __main__  argument parsing only
"""
