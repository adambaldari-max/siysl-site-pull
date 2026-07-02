"""
Mirror a Google Sites website to a local folder.

New Google Sites is a JavaScript app: a plain `wget`/`curl` only gets an empty
shell. This drives headless Chromium (Playwright) — the same toolchain the rest
of this repo uses — renders every page, discovers the in-site links, and walks
the whole site. For each page it writes:

    <outdir>/<slug>.html   fully rendered HTML (post-JavaScript)
    <outdir>/<slug>.md     extracted visible text (headings / tables / links)

plus it downloads images into <outdir>/assets/ and writes a crawl manifest to
<outdir>/_manifest.json.

------------------------------------------------------------------------------
USAGE  (run locally, on a machine with open network — NOT the Claude web sandbox)
------------------------------------------------------------------------------
    pip install -r requirements.txt
    playwright install chromium
    python mirror_site.py <published-site-url> [outdir]

The URL MUST be the **published** site link (what a visitor sees), e.g.
    https://sites.google.com/view/<name>
    https://sites.google.com/d/<siteId>/p/<pageId>/preview
    https://<your-custom-domain>/...

NOT the editor URL that ends in `/edit` — that one is private and needs a login.
To find the published link: open the site in the Google Sites editor, click
**Publish**, then copy the address under "Published site link". If the site has
never been published, see --profile below.

PRIVATE / UNPUBLISHED SITE:
    python mirror_site.py <editor-or-private-url> site_mirror --profile ~/chrome-sites
First launch Chrome once with that profile dir and log into the Google account
that owns the site:
    google-chrome --user-data-dir=~/chrome-sites
Then the crawler reuses that logged-in session so it can read private pages.
"""
import argparse
import glob as _glob
import json
import re
import sys
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

from playwright.sync_api import sync_playwright

# Reuse a sandbox-provided Chromium if present (e.g. /opt/pw-browsers), else
# fall back to Playwright's own download — same pattern as games.py.
_PRE = sorted(_glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
CHROME = _PRE[-1] if _PRE else None

IMG_EXT = re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|bmp)(\?|$)", re.I)


def slugify(url, site_root):
    """Turn a page URL into a safe, readable filename stem."""
    path = urlparse(url).path
    root_path = urlparse(site_root).path.rstrip("/")
    if path.startswith(root_path):
        path = path[len(root_path):]
    slug = path.strip("/").replace("/", "__")
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", slug)
    return slug or "index"


def in_scope(url, root_host, root_prefix):
    """Only follow links on the same host and under the site's path prefix."""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return False
    if p.netloc != root_host:
        return False
    # Keep the crawl inside this site (path prefix), but always allow the
    # site root itself.
    return p.path.startswith(root_prefix) or p.path.rstrip("/") == root_prefix.rstrip("/")


def extract_markdown(page):
    """Extract all visible page text, plus link and embedded-iframe references.

    New Google Sites renders body copy inside plain <div>/<span> nodes, so a
    tag-whitelist misses it. Using the main region's innerText captures every
    visible line regardless of markup. Links and iframes (embedded Google
    calendars / forms / docs / maps) are listed separately since those hold a
    lot of the real content and innerText can't see across an iframe boundary.
    """
    return page.evaluate(
        r"""() => {
            const parts = [];
            const main = document.querySelector('[role=main]') || document.body;
            const title = document.querySelector('h1');
            if (title) parts.push('# ' + (title.innerText || '').trim());

            // All visible text of the main content, in reading order.
            const text = (main.innerText || '').trim();
            if (text) parts.push(text);

            // Links (deduped).
            const links = new Set();
            main.querySelectorAll('a[href]').forEach(a => {
                const t = (a.innerText || '').replace(/\s+/g, ' ').trim();
                if (a.href && !a.href.startsWith('javascript:'))
                    links.add('- [' + (t || a.href) + '](' + a.href + ')');
            });
            if (links.size) parts.push('## Links\n' + [...links].join('\n'));

            // Embedded content (calendars, forms, docs, maps) lives in iframes.
            const frames = new Set();
            document.querySelectorAll('iframe[src]').forEach(f => {
                if (f.src) frames.add('- ' + f.src);
            });
            if (frames.size) parts.push('## Embedded content (iframes)\n' + [...frames].join('\n'));

            return parts.join('\n\n');
        }"""
    )


def discover_links(page):
    return page.evaluate(
        "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)"
    )


def discover_images(page):
    return page.evaluate(
        """() => {
            const urls = new Set();
            document.querySelectorAll('img[src]').forEach(i => urls.add(i.src));
            document.querySelectorAll('[style*="background-image"]').forEach(e => {
                const m = (e.getAttribute('style')||'').match(/url\\((['"]?)(.*?)\\1\\)/);
                if (m) urls.add(m[2]);
            });
            return Array.from(urls);
        }"""
    )


def main():
    ap = argparse.ArgumentParser(description="Mirror a Google Sites website locally.")
    ap.add_argument("url", help="Published site URL (the public link, not the /edit editor URL)")
    ap.add_argument("outdir", nargs="?", default="site_mirror", help="Output folder (default: site_mirror)")
    ap.add_argument("--profile", help="Chrome user-data-dir already logged in (for private/unpublished sites)")
    ap.add_argument("--max-pages", type=int, default=200, help="Safety cap on pages to crawl")
    ap.add_argument("--no-images", action="store_true", help="Skip downloading images")
    ap.add_argument("--wait", type=int, default=3500, help="ms to wait for JS render per page")
    args = ap.parse_args()

    start = urldefrag(args.url)[0]
    if start.rstrip("/").endswith("/edit"):
        print("WARNING: that looks like the private editor URL (ends in /edit).")
        print("         Anonymous crawling needs the PUBLISHED link, or pass --profile")
        print("         with a Chrome profile logged into the owner account.\n")
    parsed = urlparse(start)
    root_host = parsed.netloc
    # Scope the crawl to the directory the start URL lives in.
    root_prefix = parsed.path.rsplit("/", 1)[0] + "/" if "/" in parsed.path else "/"

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    assets = out / "assets"
    assets.mkdir(exist_ok=True)

    manifest = {"start": start, "pages": [], "images": []}
    seen_imgs = set()

    with sync_playwright() as p:
        launch_kw = {"headless": True, "args": ["--no-sandbox"]}
        if CHROME:
            launch_kw["executable_path"] = CHROME
        if args.profile:
            ctx = p.chromium.launch_persistent_context(
                args.profile, ignore_https_errors=True, **launch_kw)
            browser = None
        else:
            browser = p.chromium.launch(**launch_kw)
            ctx = browser.new_context(ignore_https_errors=True)
        page = ctx.new_page()
        api = ctx.request  # for downloading image bytes with the same session

        queue = deque([start])
        visited = set()

        while queue and len(visited) < args.max_pages:
            url = queue.popleft()
            if url in visited:
                continue
            visited.add(url)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(args.wait)
            except Exception as e:
                print(f"  ! failed: {url}  ({e})")
                continue

            slug = slugify(url, start)
            title = (page.title() or "").strip()
            (out / f"{slug}.html").write_text(page.content(), encoding="utf-8")
            try:
                (out / f"{slug}.md").write_text(extract_markdown(page), encoding="utf-8")
            except Exception:
                pass
            manifest["pages"].append({"url": url, "title": title, "slug": slug})
            print(f"  [{len(visited)}] {title or slug}  <- {url}")

            # Queue same-site links.
            for link in discover_links(page):
                link = urldefrag(urljoin(url, link))[0]
                if link not in visited and in_scope(link, root_host, root_prefix):
                    queue.append(link)

            # Download images.
            if not args.no_images:
                for img in discover_images(page):
                    img = urljoin(url, img)
                    if img in seen_imgs:
                        continue
                    seen_imgs.add(img)
                    name = re.sub(r"[^A-Za-z0-9._-]", "-", urlparse(img).path.split("/")[-1]) or "img"
                    if not IMG_EXT.search(name):
                        name += ".img"
                    try:
                        resp = api.get(img, timeout=30000)
                        if resp.ok:
                            (assets / name).write_bytes(resp.body())
                            manifest["images"].append({"url": img, "file": f"assets/{name}"})
                    except Exception:
                        pass

        ctx.close()
        if browser:
            browser.close()

    (out / "_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("\n" + "=" * 60)
    print(f"Mirrored {len(manifest['pages'])} page(s), {len(manifest['images'])} image(s)")
    print(f"Output -> {out.resolve()}")
    print(f"  pages : {out}/<slug>.html  (+ .md text)")
    print(f"  images: {assets}/")
    print(f"  index : {out}/_manifest.json")
    if not manifest["pages"]:
        print("\nNo pages captured. If this is a private/unpublished site, re-run with")
        print("  --profile <chrome-user-data-dir>  logged into the owner account.")


if __name__ == "__main__":
    main()
