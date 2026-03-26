"""Bootstrap a random filesystem on a WebDAV server via OpenDAL."""

import argparse
import random

from faker import Faker
from opendal import Operator

fake = Faker()


def generate_markdown() -> bytes:
    return f"# {fake.catch_phrase()}\n{fake.text(300)}\n".encode()


def populate(
    op: Operator,
    path: str,
    depth: int,
    folders: int,
    files: int,
    manifest: list[str],
):
    for _ in range(files):
        name = fake.slug() + ".md"
        file_path = f"{path}{name}"
        op.write(file_path, generate_markdown())
        manifest.append(f"/{file_path}")

    if depth <= 0:
        return

    for _ in range(folders):
        folder = fake.slug() + "/"
        populate(op, f"{path}{folder}", depth - 1, folders, files, manifest)


def main():
    parser = argparse.ArgumentParser(description="Create a random filesystem on WebDAV")
    parser.add_argument(
        "--endpoint",
        default="http://localhost:8090/dav",
        help="WebDAV server URL",
    )
    parser.add_argument("--username", default="admin", help="WebDAV username")
    parser.add_argument("--password", default="admin", help="WebDAV password")
    parser.add_argument("--root", default="/", help="Root path on the server")
    parser.add_argument(
        "--depth",
        type=int,
        default=3,
        help="Max directory nesting depth",
    )
    parser.add_argument(
        "--folders",
        type=int,
        default=3,
        help="Folders per directory",
    )
    parser.add_argument(
        "--files",
        type=int,
        default=5,
        help="Files per directory",
    )
    parser.add_argument("--seed", type=int, default=None, help="Random seed")
    args = parser.parse_args()

    if args.seed is not None:
        Faker.seed(args.seed)
        random.seed(args.seed)

    op = Operator(
        "webdav",
        endpoint=args.endpoint,
        username=args.username,
        password=args.password,
        root=args.root,
    )

    from urllib.parse import urlparse

    uri_path = urlparse(args.endpoint).path.rstrip("/")
    manifest: list[str] = []
    populate(op, "", args.depth, args.folders, args.files, manifest)

    absolute = [f"{uri_path}{entry}" for entry in manifest]
    op.write("manifest.txt", ("\n".join(absolute) + "\n").encode())

    print(f"Created {len(manifest)} files across the tree")
    print("Wrote manifest.txt")


if __name__ == "__main__":
    main()
