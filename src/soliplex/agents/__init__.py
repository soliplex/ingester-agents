#
__version__ = "0.1.0"


class ValidationError(Exception):
    def __init__(self, config):
        super().__init__(f"Invalid config: {config}")


class UrlsFileFormatError(ValueError):
    """A URL list file whose content isn't a URL list.

    Raised for an HTML document, a body that isn't UTF-8 text, or a non-empty
    file with no valid line left once comments and invalid lines are dropped.
    Raising (rather than returning an empty list) fails the component, so the
    stale-document clean-up is skipped instead of wiping the source.
    """
