"""Custom diagnostic / build tools for Tank Exporter PY.

Each module in this package is independently runnable as a CLI script
(see the `if __name__ == '__main__': main()` block at the bottom of
each file).  Some are also imported in-process by the viewer so the
same logic can be triggered from a UI button -- see
`viewer._on_rebuild_itemlist_clicked` for the rebuild-itemlist case.
"""
