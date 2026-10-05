#
__version__ = "0.1.0"


class ValidationError(Exception):
    def __init__(self, config):
        super().__init__(f"Invalid config: {config}")


class WebDAVListingError(Exception):
    """A WebDAV walk could not list one or more of its subtrees.

    Raised by strict listings, whose output must be complete: an export or a
    report built from a partial listing could later drive deletions.
    """

    def __init__(self, path: str, errors: list[dict]):
        self.path = path
        self.errors = errors
        failed = ", ".join(e["path"] for e in errors)
        super().__init__(f"WebDAV listing of {path} incomplete: {len(errors)} subtree(s) failed: {failed}")


class EmptyComponentError(Exception):
    """A component with ``error_on_empty`` set found nothing to ingest.

    Raised by the manifest runner, so the component counts as failed and the
    stale-document clean-up is skipped instead of deleting the whole source.
    """

    def __init__(self, component: str, component_type: str, source: str, inventory: int, not_found: int):
        self.component = component
        self.component_type = component_type
        self.source = source
        self.inventory = inventory
        self.not_found = not_found
        super().__init__(
            f"Component '{component}' ({component_type}) returned no items (after extension filtering) "
            f"and has error_on_empty set; skipping stale-document removal for source '{source}' "
            f"(inventory={inventory}, not_found={not_found})"
        )


class UrlsFileFormatError(ValueError):
    """A URL list file whose content isn't a URL list.

    Raised for an HTML document, a body that isn't UTF-8 text, or a non-empty
    file with no valid line left once comments and invalid lines are dropped.
    Raising (rather than returning an empty list) fails the component, so the
    stale-document clean-up is skipped instead of wiping the source.
    """
