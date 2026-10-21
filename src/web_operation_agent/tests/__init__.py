def browser_only(test):
    """Mark tests requiring real Chromium, independently of installed prerequisites."""
    test.__woa_browser_test__ = True
    return test
