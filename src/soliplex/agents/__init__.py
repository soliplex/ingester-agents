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
