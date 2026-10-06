"""Abstract base class for SCM (Source Control Management) providers."""

import asyncio
import base64
import datetime
import logging
import random
from abc import ABC
from abc import abstractmethod
from collections.abc import AsyncIterator
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
from tenacity import AsyncRetrying

from soliplex.agents.common import mime
from soliplex.agents.common.mime import passes_extension_prefilter
from soliplex.agents.config import settings
from soliplex.agents.retry import RETRYABLE_STATUS_CODES
from soliplex.agents.retry import RetryableHTTPError
from soliplex.agents.retry import parse_retry_after
from soliplex.agents.retry import retry_policy
from soliplex.agents.scm import APIFetchError
from soliplex.agents.scm import CursorNotFound
from soliplex.agents.scm import SCMException
from soliplex.agents.scm.lib.utils import compute_file_hash
from soliplex.agents.scm.lib.utils import decode_base64_if_needed
from soliplex.agents.scm.lib.utils import flatten_list

logger = logging.getLogger(__name__)


class BaseSCMProvider(ABC):
    """Abstract base class for SCM providers (GitHub, Gitea, etc.)."""

    def __init__(self, owner: str | None = None):
        """
        Initialize SCM provider.

        Args:
            owner: Default repository owner
        """
        self.owner = owner

    def get_base_url(self) -> str:
        """Get the base API URL for this provider."""
        if settings.scm_base_url is None:
            raise SCMException("SCM base URL is not configured")
        return settings.scm_base_url

    def get_auth_token(self) -> str:
        """Get the authentication token from settings."""
        return settings.scm_auth_token

    def get_auth_headers(self) -> dict[str, str]:
        """
        Get authentication headers for HTTP requests.

        Supports both token-based and basic authentication.
        Priority: token authentication > basic authentication

        Returns:
            Dictionary with Authorization header

        Raises:
            AuthenticationConfigError: If no valid authentication is configured
        """
        from soliplex.agents.scm import AuthenticationConfigError

        # Priority 1: Token authentication
        if settings.scm_auth_token is not None:
            return {"Authorization": f"token {settings.scm_auth_token.get_secret_value()}"}

        # Priority 2: Basic authentication
        if settings.scm_auth_username and settings.scm_auth_password:
            credentials = f"{settings.scm_auth_username}:{settings.scm_auth_password.get_secret_value()}"
            encoded = base64.b64encode(credentials.encode()).decode()
            return {"Authorization": f"Basic {encoded}"}

        # No valid authentication configured
        raise AuthenticationConfigError

    @asynccontextmanager
    async def get_session(self):
        """Create an authenticated HTTP session with timeout configuration."""
        timeout = aiohttp.ClientTimeout(
            total=settings.http_timeout_total,
            connect=settings.http_timeout_connect,
            sock_read=settings.http_timeout_sock_read,
        )
        connector = aiohttp.TCPConnector(ssl=settings.ssl_verify)
        headers = self.get_auth_headers()
        async with aiohttp.ClientSession(headers=headers, connector=connector, timeout=timeout) as session:
            yield session

    def build_url(self, path: str) -> str:
        """
        Build full URL from base URL and path.

        Args:
            path: API endpoint path

        Returns:
            Full URL
        """
        base_url = self.get_base_url().rstrip("/")
        path = path.lstrip("/")
        return f"{base_url}/{path}"

    async def _request_with_retry(
        self,
        session: aiohttp.ClientSession,
        url: str,
        semaphore: asyncio.Semaphore | None = None,
    ) -> aiohttp.ClientResponse:
        """Perform an HTTP GET with tenacity retry, rate-limit and 5xx handling.

        Args:
            session: Active aiohttp session.
            url: URL to GET.
            semaphore: Optional concurrency limiter.

        Returns:
            The successful ``aiohttp.ClientResponse``.

        Raises:
            RetryableHTTPError: After all retries exhausted on 429/5xx.
            aiohttp.ClientError / TimeoutError: After all retries exhausted.
        """
        policy = retry_policy(
            max_attempts=settings.scm_retry_attempts,
            max_delay=settings.scm_retry_backoff_max,
        )
        async for attempt in AsyncRetrying(**policy):
            with attempt:
                await asyncio.sleep(random.uniform(0.01, 0.05))
                if semaphore:
                    async with semaphore:
                        resp = await session.get(url)
                else:
                    resp = await session.get(url)

                if resp.status in RETRYABLE_STATUS_CODES:
                    ra = parse_retry_after(resp.headers)
                    body = ""
                    try:
                        body = await resp.text()
                    except Exception:  # noqa: BLE001
                        pass
                    resp.release()
                    raise RetryableHTTPError(resp.status, retry_after=ra, body=body)

                return resp

        raise AssertionError("unreachable")  # pragma: no cover

    async def paginate(
        self, url_template: str, owner: str, repo: str, process_response: Callable | None = None
    ) -> list[dict[str, Any]]:
        """
        Paginate through API responses with session reuse and retry logic.

        Args:
            url_template: URL template with {page} placeholder
            owner: Repository owner
            repo: Repository name
            process_response: Optional function to process each response

        Returns:
            List of all items from all pages
        """
        ret = []
        items = []
        page = 1

        async with self.get_session() as session:
            while len(items) != 0 or page == 1:
                url = url_template.format(owner=owner, repo=repo, page=page)
                logger.info(f"fetching page={page} {owner}/{repo}")

                response = await self._request_with_retry(session, url)
                async with response:
                    if response.status == 404:
                        msg = f"repo {owner}/{repo} not found"
                        raise SCMException(msg)

                    items = await response.json()

                    if response.status != 200:
                        if "errors" in items:
                            raise SCMException(str(items["errors"]))
                        logger.error("Failed to fetch from %s: %s", url, items)
                        raise APIFetchError

                    if process_response:
                        items = process_response(items)

                    logger.info(f"found {len(items)} items on page {page}")
                    ret.extend(items)
                    page += 1

        return ret

    async def list_issues(
        self, repo: str, owner: str | None = None, add_comments: bool = False, since: datetime.datetime | None = None
    ) -> list[dict[str, Any]]:
        """
        List the open issues of a repository.

        Only open issues are listed (``state=open``, which is also both
        GitHub's and Gitea's default): a closed issue drops out of the list,
        and so out of the source once its removal is confirmed.

        Args:
            repo: Repository name
            owner: Repository owner (defaults to instance owner)
            add_comments: Whether to include comments for each issue

        Returns:
            List of issue dictionaries
        """
        owner = owner or self.owner
        url_template = self.build_url("/repos/{owner}/{repo}/issues?page={page}&state=open")
        if since:
            # Gitea expects an RFC3339 timestamp. isoformat() on a tz-aware
            # datetime emits a "+00:00" offset which, combined with a trailing
            # "Z", is invalid; the unescaped "+" also decodes to a space
            # server-side. Normalise to UTC and emit a single "Z".
            since_utc = since if since.tzinfo is None else since.astimezone(datetime.UTC)
            url_template += f"&since={since_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        issues = await self.paginate(url_template, owner, repo)

        if add_comments:
            if since is None:
                comments = await self.list_repo_comments(owner, repo)
                for issue in issues:
                    issue["comments"] = [comment["body"] for comment in comments if comment["issue_url"] == issue["url"]]
                    issue["comment_count"] = len(issue["comments"])
            else:
                for issue in issues:
                    issue["comments"] = await self.list_issue_comments(owner, repo, issue["number"])
                    issue["comment_count"] = len(issue["comments"])

        return issues

    async def list_repo_comments(self, owner: str | None, repo: str) -> list[dict[str, Any]]:
        """
        List all issue comments for a repository.

        Args:
            owner: Repository owner
            repo: Repository name

        Returns:
            List of comment dictionaries
        """
        owner = owner or self.owner
        url_template = self.build_url("/repos/{owner}/{repo}/issues/comments?page={page}")
        return await self.paginate(url_template, owner, repo)

    async def list_issue_comments(self, owner: str | None, repo: str, issue_number: int) -> list[dict[str, Any]]:
        """
        List comments for a specific issue.

        Args:
            owner: Repository owner
            repo: Repository name
            issue_number: Issue number

        Returns:
            List of comment dictionaries
        """
        owner = owner or self.owner
        url = self.build_url(f"/repos/{owner}/{repo}/issues/{issue_number}/comments")
        return await self._fetch_json(url)

    async def get_repo(self, repo: str, owner: str | None = None) -> dict[str, Any]:
        """
        Fetch a repository's record.

        Raises on anything but success -- including the 404 a provider gives
        for a private repository the credentials cannot see, which is why it
        is checked before any issue 404 is believed.

        Args:
            repo: Repository name
            owner: Repository owner (defaults to instance owner)

        Returns:
            The repository record
        """
        owner = owner or self.owner
        return await self._fetch_json(self.build_url(f"/repos/{owner}/{repo}"))

    async def get_default_branch(self, repo: str, owner: str | None = None) -> str:
        """Return the repository's default branch."""
        rec = await self.get_repo(repo, owner)
        branch = rec.get("default_branch") if isinstance(rec, dict) else None
        if not branch:
            raise SCMException(f"repository {owner or self.owner}/{repo} reports no default branch")
        return branch

    async def get_issue_state(self, repo: str, owner: str | None, number: int) -> str:
        """
        Ask the API whether one issue is still there.

        Used to confirm that an issue missing from the issue list really is
        gone before its document is removed.

        Args:
            repo: Repository name
            owner: Repository owner (defaults to instance owner)
            number: Issue number

        Returns:
            ``"gone"`` (404), or the issue's ``state`` (``"open"`` / ``"closed"``)

        Raises:
            SCMException / aiohttp.ClientResponseError: On any other answer
        """
        owner = owner or self.owner
        url = self.build_url(f"/repos/{owner}/{repo}/issues/{number}")
        async with self.get_session() as session:
            response = await self._request_with_retry(session, url)
            async with response:
                if self._issue_is_gone(response):
                    return "gone"
                response.raise_for_status()
                issue = await response.json()
        state = issue.get("state") if isinstance(issue, dict) else None
        if state not in ("open", "closed"):
            raise SCMException(f"issue {owner}/{repo}#{number} has unexpected state {state!r}")
        return state

    def _issue_is_gone(self, response: aiohttp.ClientResponse) -> bool:
        """Whether a single-issue GET says the issue no longer exists here."""
        return response.status == 404

    def parse_file_rec(self, rec: dict[str, Any]) -> dict[str, Any]:
        """
        Parse a file record from the API response.

        Args:
            rec: File record from API

        Returns:
            Normalized file dictionary with metadata
        """
        file_bytes = decode_base64_if_needed(rec["content"])
        file_hash = compute_file_hash(file_bytes)
        uri = rec["path"]

        return {
            "name": rec["name"],
            "url": rec["url"],
            # Browsable location, as opposed to the API "url" above. Read
            # with .get because the listing and blob-fetch shapes do not all
            # carry it; a missing one simply leaves the sidecar without a URL.
            "html_url": rec.get("html_url"),
            "uri": uri,
            "path": uri,
            "file_bytes": file_bytes,
            "sha256": file_hash,
            "content-type": mime.detect_mime_type(rec["name"], data=file_bytes, text_fallback=True),
            "last_updated": self.get_last_updated(rec),
            # Gitea's contents API carries it; GitHub's does not.
            "last_commit_sha": rec.get("last_commit_sha"),
        }

    @abstractmethod
    def get_last_updated(self, rec: dict[str, Any]) -> str | None:
        """
        Extract last updated timestamp from file record.

        Args:
            rec: File record from API

        Returns:
            Last updated timestamp or None if not available
        """
        pass  # pragma: no cover

    async def get_file_content(
        self, rec: dict[str, Any], session: aiohttp.ClientSession, owner: str, repo: str
    ) -> dict[str, Any]:
        """
        Get file content, handling special cases like empty content.

        Default implementation returns the record as-is. Override for provider-specific behavior.

        Args:
            rec: File record
            session: HTTP session
            owner: Repository owner
            repo: Repository name

        Returns:
            Updated file record with content
        """
        return rec

    async def _fetch_json(self, url: str) -> dict[str, Any] | list[dict[str, Any]]:
        """
        Fetch JSON from URL with retry logic and rate limiting.

        This is a simple helper for fetching JSON data from API endpoints
        that don't require pagination or complex processing.

        Args:
            url: Full API URL to fetch

        Returns:
            Parsed JSON response (dict or list)

        Raises:
            aiohttp.ClientError: If request fails after all retries
            TimeoutError: If request times out after all retries
            RetryableHTTPError: If server returns 429/5xx after all retries
        """
        logger.debug(f"_fetch_json url={url}")

        async with self.get_session() as session:
            response = await self._request_with_retry(session, url)
            async with response:
                response.raise_for_status()
                return await response.json()

    async def get_data_from_url(
        self,
        url: str,
        session: aiohttp.ClientSession,
        owner: str | None = None,
        repo: str | None = None,
        allowed_extensions: list[str] | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """
        Recursively fetch data from API URL with concurrency control and retry logic.

        Args:
            url: API URL to fetch
            session: HTTP session
            owner: Repository owner (optional, for provider-specific handling)
            repo: Repository name (optional, for provider-specific handling)
            allowed_extensions: List of allowed file extensions
            semaphore: Optional semaphore for concurrency limiting

        Returns:
            Parsed file record or list of records. A file or directory that
            cannot be fetched becomes an error row, ``{"uri": url, "url": url,
            "error": ...}``, never a silent gap: a gap reads as a deletion.
        """
        logger.debug(f"get_data_from_url = {url}")

        try:
            response = await self._request_with_retry(session, url, semaphore)
            async with response:
                response.raise_for_status()
                res = await response.json()

            if isinstance(res, dict):
                # This is a file, fetch content if needed and parse
                if owner and repo:
                    res = await self.get_file_content(res, session, owner, repo)
                return self.parse_file_rec(res)
            else:
                # This is a directory, recursively fetch all files
                parsed = []
                for r in res:
                    if passes_extension_prefilter(r["name"], allowed_extensions):
                        logger.debug(f"fetching file in dir for url = {r['url']}")
                        parsed.append(
                            await self.get_data_from_url(r["url"], session, owner, repo, allowed_extensions, semaphore)
                        )
                    else:
                        logger.debug(f"ignoring {r['name']} in dir for url = {r['url']}")

                return parsed

        except Exception as e:
            logger.exception("Error fetching from %s", url)
            return {"uri": url, "url": url, "error": str(e)}

    async def list_repo_files(
        self,
        repo: str,
        owner: str | None = None,
        allowed_extensions: list[str] | None = None,
        branch: str = "main",
    ) -> list[dict[str, Any]]:
        """
        List all files in a repository with concurrency control.

        Args:
            repo: Repository name
            owner: Repository owner (defaults to instance owner)
            allowed_extensions: List of allowed file extensions
            branch: Branch name

        Returns:
            List of file dictionaries
        """
        owner = owner or self.owner
        allowed_extensions = allowed_extensions or settings.extensions

        # Create semaphore to limit concurrent requests
        semaphore = asyncio.Semaphore(settings.scm_max_concurrent_requests)

        async with self.get_session() as session:
            resp = await self._list_root(session, owner, repo, branch)

            files = [x for x in resp if x["type"] == "file"]
            dirs = [x for x in resp if x["type"] == "dir"]
            logger.debug(f"dirs={[(x['name'], x['type']) for x in resp]}")

            tasks = [
                self.get_data_from_url(file["url"], session, owner, repo, None, semaphore)
                for file in files
                if passes_extension_prefilter(file["name"], allowed_extensions)
            ]
            for dir in dirs:
                tasks.append(self.get_data_from_url(dir["url"], session, owner, repo, allowed_extensions, semaphore))

            ret = await asyncio.gather(*tasks)
            ret = flatten_list(ret)
            logger.info(f"found {len(ret)} files in {repo}")
            return ret

    async def _list_root(self, session: aiohttp.ClientSession, owner: str, repo: str, branch: str) -> list[dict[str, Any]]:
        """
        List the top level of *branch*, strictly.

        Anything but a 200 with a JSON list raises, after the usual retries,
        except a 404 the provider recognises as an existing but empty branch
        (:meth:`is_empty_branch`), which lists as ``[]``.

        Raises:
            SCMException / RetryableHTTPError: On any other answer
        """
        url = self.build_url(f"/repos/{owner}/{repo}/contents?ref={branch}")
        logger.debug(f"url = {url}")

        response = await self._request_with_retry(session, url)
        async with response:
            status = response.status
            try:
                resp = await response.json()
            except (aiohttp.ContentTypeError, ValueError) as e:
                raise SCMException(f"listing {owner}/{repo} at '{branch}': HTTP {status}, response is not JSON") from e

        if status == 404 and await self.is_empty_branch(session, owner, repo, branch, resp):
            logger.info(f"Repository {owner}/{repo} has no files on branch {branch}, returning empty file list")
            return []
        await self.validate_response(response, resp)
        if status != 200 or not isinstance(resp, list):
            raise SCMException(f"listing {owner}/{repo} at '{branch}': unexpected response (HTTP {status})")
        return resp

    async def is_empty_branch(self, session: aiohttp.ClientSession, owner: str, repo: str, branch: str, resp: Any) -> bool:
        """
        Whether a 404 listing *branch* means it exists and has no files.

        The default is no: a 404 is an error. A provider that answers 404 for
        an empty tree overrides this, and should raise when the real cause is
        a missing branch, so that never lists as an empty repository.
        """
        return False

    async def _branch_exists(self, session: aiohttp.ClientSession, owner: str, repo: str, branch: str) -> bool:
        """``GET /repos/{owner}/{repo}/branches/{branch}``: 200 is yes, 404 no, anything else raises."""
        url = self.build_url(f"/repos/{owner}/{repo}/branches/{branch}")
        response = await self._request_with_retry(session, url)
        async with response:
            if response.status == 404:
                return False
            response.raise_for_status()
            return True

    async def iter_repo_files(
        self, repo: str, owner: str | None = None, branch: str = "main"
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Iterate through repository files with concurrency control.

        Args:
            repo: Repository name
            owner: Repository owner (defaults to instance owner)
            branch: Branch name

        Yields:
            File dictionaries
        """
        owner = owner or self.owner

        # Create semaphore to limit concurrent requests
        semaphore = asyncio.Semaphore(settings.scm_max_concurrent_requests)

        async with self.get_session() as session:
            resp = await self._list_root(session, owner, repo, branch)

            files = [x for x in resp if x["type"] == "file"]
            dirs = [x for x in resp if x["type"] == "dir"]
            logger.debug(f"dirs={[(x['name'], x['type']) for x in resp]}")

            tasks = [self.get_data_from_url(file["url"], session, owner, repo, None, semaphore) for file in files]
            for dir in dirs:
                tasks.append(self.get_data_from_url(dir["url"], session, owner, repo, None, semaphore))

            ct = 0
            for task in tasks:
                ret = await task
                # Handle both single files and lists
                items = ret if isinstance(ret, list) else [ret]
                for item in flatten_list(items):
                    ct += 1
                    yield item

            logger.info(f"found {ct} files in {repo}")

    async def validate_response(self, response: aiohttp.ClientResponse, resp: dict | list) -> None:
        """
        Validate API response and raise exceptions if needed.

        Default implementation checks for 'errors' key. Override for provider-specific validation.

        Args:
            response: HTTP response
            resp: Parsed JSON response

        Raises:
            SCMException: If response indicates an error
        """
        if isinstance(resp, dict) and "errors" in resp:
            raise SCMException(str(resp))

    async def list_commits_since(
        self,
        repo: str,
        owner: str | None = None,
        since_commit_sha: str | None = None,
        branch: str = "main",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """
        List commits since a specific commit SHA.

        Args:
            repo: Repository name
            owner: Repository owner
            since_commit_sha: SHA of last processed commit (None = get all recent)
            branch: Branch to fetch from
            limit: Maximum commits to fetch per page

        Returns:
            List of commit objects, newest first

        Raises:
            CursorNotFound: If *since_commit_sha* is given but never found --
                the history ran out (a force-push) or ``max_pages`` was
                reached -- so the commits listed are not known to be all the
                new ones.
        """
        owner = owner or self.owner
        url = self._commits_url(owner, repo, branch, limit)

        logger.debug(f"Fetching commits from {url}")

        commits = []
        found_marker = False

        async with self.get_session() as session:
            page = 1
            max_pages = 10  # Safety limit

            while page <= max_pages and not found_marker:
                paginated_url = f"{url}&page={page}"

                response = await self._request_with_retry(session, paginated_url)
                async with response:
                    resp = await response.json()
                    await self.validate_response(response, resp)

                page_commits = resp if isinstance(resp, list) else []

                if not page_commits:
                    break  # No more commits

                for commit in page_commits:
                    if since_commit_sha and commit.get("sha") == since_commit_sha:
                        found_marker = True
                        break
                    commits.append(commit)

                if len(page_commits) < limit:
                    break

                page += 1

        if since_commit_sha and not found_marker:
            raise CursorNotFound(since_commit_sha)

        logger.info(f"Found {len(commits)} new commits since {since_commit_sha or 'beginning'}")
        return commits

    def _commits_url(self, owner: str, repo: str, branch: str, limit: int) -> str:
        """The commit-list URL for *branch*, *limit* per page (Gitea's ``limit``)."""
        return self.build_url(f"/repos/{owner}/{repo}/commits?sha={branch}&limit={limit}")

    async def get_commit_details(
        self, repo: str, owner: str | None = None, commit_sha: str = None, branch: str = "main"
    ) -> dict[str, Any]:
        """
        Get detailed commit information including file changes.

        Args:
            repo: Repository name
            owner: Repository owner
            commit_sha: Commit SHA
            branch: Branch the commit was listed from (unused here: a SHA is
                global to the API; the git CLI reads it from that branch's
                checkout)

        Returns:
            Commit object with files list
        """
        owner = owner or self.owner
        url = self.build_url(f"/repos/{owner}/{repo}/git/commits/{commit_sha}")
        return await self._fetch_json(url)

    async def get_single_file(
        self, repo: str, owner: str | None = None, file_path: str = "", branch: str = "main"
    ) -> dict[str, Any]:
        """
        Get a single file from repository.

        Args:
            repo: Repository name
            owner: Repository owner
            file_path: Path to file in repository
            branch: Branch name

        Returns:
            Parsed file object with content
        """
        owner = owner or self.owner
        # URL encode the file path
        from urllib.parse import quote

        encoded_path = quote(file_path, safe="")
        url = self.build_url(f"/repos/{owner}/{repo}/contents/{encoded_path}?ref={branch}")

        resp = await self._fetch_json(url)
        return self.parse_file_rec(resp)

    async def create_repository(
        self,
        name: str,
        description: str = "",
        private: bool = False,
        organization: str | None = None,
    ) -> dict[str, Any]:
        """
        Create a new repository.

        If owner is specified and differs from the authenticated user, creates the repository
        under that organization. Otherwise creates it under the authenticated user.

        Args:
            name: Repository name
            description: Repository description
            private: Whether the repository should be private
            organization: Organization name to create repo under (optional)

        Returns:
            Dictionary containing the created repository information

        Raises:
            SCMException: If repository creation fails
        """

        # Build the appropriate URL based on whether we're creating for an org or user
        if organization:
            url = self.build_url(f"/orgs/{organization}/repos")
        else:
            url = self.build_url("/user/repos")

        payload = {
            "name": name,
            "description": description,
            "private": private,
        }
        owner = self.owner

        async with self.get_session() as session:
            async with session.post(url, json=payload) as response:
                resp = await response.json()

                if response.status == 201:
                    logger.info(f"Created repository: {name}")
                    return resp
                elif response.status == 409:
                    msg = f"Repository '{name}' already exists"
                    raise SCMException(msg)
                elif response.status == 404:
                    msg = f"Organization '{organization}' or user '{owner}' not found"
                    raise SCMException(msg)
                elif response.status == 403:
                    msg = f"Permission denied to create repository under '{owner}'"
                    raise SCMException(msg)
                else:
                    if isinstance(resp, dict) and "message" in resp:
                        raise SCMException(resp["message"])
                    raise SCMException(f"Failed to create repository: {response.status}")

    async def delete_repository(self, repo: str, owner: str | None = None) -> bool:
        """
        Delete a repository.

        Args:
            repo: Repository name
            owner: Repository owner (defaults to instance owner)

        Returns:
            True if deletion was successful

        Raises:
            SCMException: If repository deletion fails
        """
        owner = owner or self.owner
        url = self.build_url(f"/repos/{owner}/{repo}")

        async with self.get_session() as session:
            async with session.delete(url) as response:
                if response.status == 204:
                    logger.info(f"Deleted repository: {owner}/{repo}")
                    return True
                elif response.status == 404:
                    msg = f"Repository '{owner}/{repo}' not found"
                    raise SCMException(msg)
                elif response.status == 403:
                    msg = f"Permission denied to delete repository '{owner}/{repo}'"
                    raise SCMException(msg)
                else:
                    resp = await response.json()
                    if isinstance(resp, dict) and "message" in resp:
                        raise SCMException(resp["message"])
                    raise SCMException(f"Failed to delete repository: {response.status}")

    async def create_issue(
        self,
        repo: str,
        title: str,
        body: str = "",
        owner: str | None = None,
    ) -> dict[str, Any]:
        """
        Create a new issue in a repository.

        Args:
            repo: Repository name
            title: Issue title
            body: Issue body/description
            owner: Repository owner (defaults to instance owner)

        Returns:
            Dictionary containing the created issue information

        Raises:
            SCMException: If issue creation fails
        """
        owner = owner or self.owner
        url = self.build_url(f"/repos/{owner}/{repo}/issues")

        payload = {
            "title": title,
            "body": body,
        }

        async with self.get_session() as session:
            async with session.post(url, json=payload) as response:
                resp = await response.json()

                if response.status == 201:
                    logger.info(f"Created issue '{title}' in {owner}/{repo}")
                    return resp
                elif response.status == 404:
                    msg = f"Repository '{owner}/{repo}' not found"
                    raise SCMException(msg)
                elif response.status == 403:
                    msg = f"Permission denied to create issue in '{owner}/{repo}'"
                    raise SCMException(msg)
                else:
                    if isinstance(resp, dict) and "message" in resp:
                        raise SCMException(resp["message"])
                    raise SCMException(f"Failed to create issue: {response.status}")

    async def create_file(
        self,
        repo: str,
        file_path: str,
        content: bytes | str,
        message: str = "Add file",
        branch: str = "main",
        owner: str | None = None,
    ) -> dict[str, Any]:
        """
        Create or update a file in a repository.

        Args:
            repo: Repository name
            file_path: Path to the file in the repository
            content: File content (bytes or string)
            message: Commit message
            branch: Branch name (default: main)
            owner: Repository owner (defaults to instance owner)

        Returns:
            Dictionary containing the commit information

        Raises:
            SCMException: If file creation fails
        """
        import base64

        owner = owner or self.owner
        url = self.build_url(f"/repos/{owner}/{repo}/contents/{file_path}")

        # Encode content to base64
        if isinstance(content, str):
            content_bytes = content.encode("utf-8")
        else:
            content_bytes = content
        content_b64 = base64.b64encode(content_bytes).decode("ascii")

        payload = {
            "content": content_b64,
            "message": message,
            "branch": branch,
        }

        async with self.get_session() as session:
            async with session.post(url, json=payload) as response:
                resp = await response.json()

                if response.status in (200, 201):
                    logger.info(f"Created file '{file_path}' in {owner}/{repo}")
                    return resp
                elif response.status == 404:
                    msg = f"Repository '{owner}/{repo}' not found"
                    raise SCMException(msg)
                elif response.status == 403:
                    msg = f"Permission denied to create file in '{owner}/{repo}'"
                    raise SCMException(msg)
                elif response.status == 422:
                    msg = f"File '{file_path}' already exists or invalid request"
                    raise SCMException(msg)
                else:
                    if isinstance(resp, dict) and "message" in resp:
                        raise SCMException(resp["message"])
                    raise SCMException(f"Failed to create file: {response.status}")
