# Deployment

`dist/index.html` is the entire deployable artifact: a single static HTML
file with CSS, JS, and the month's metrics all inlined. There is no server,
no database, no environment config, and no runtime API calls to configure
for production — whatever renders correctly when opened locally will
render identically wherever you put it.

Build it first if you haven't:

```bash
python3 -m collector.collect   # writes data/metrics.json
python3 site/build.py          # writes dist/index.html
```

## Option 1 — Serve as a static file (simplest)

Copy `dist/index.html` to any static file host or web server and point it
at that file (or rename it and route `/` to it). This works with, e.g., an
S3 bucket, Nginx, any CDN, or just opening the file directly in a browser
for local viewing. No build step, no server-side runtime, nothing else to
configure.

## Option 2 — GitHub Pages

This mirrors the visual-language precedent already in this project's
history (`jackhamr-doc-site`, used as the dashboard's design-token source)
and matches `spec.md`'s deployment note: "deployable to GitHub Pages or any
static host."

Once this project is pushed to a GitHub repo, the simplest route is
serving Pages from a `docs/` folder or a dedicated branch containing only
the built file:

```bash
# from the project root, after building dist/index.html
git checkout --orphan gh-pages
git rm -rf .
cp dist/index.html index.html
git add index.html
git commit -m "Deploy dashboard"
git push origin gh-pages
```

Then in the repo's GitHub Settings -> Pages, set the source to the
`gh-pages` branch, root folder.

**Note on `.gitignore`**: this project's `.gitignore` excludes `dist/*`
(the collector's raw output and generated build artifacts aren't meant to
be committed to the main branch). That's irrelevant on an orphan
`gh-pages` branch created as above, since it starts with an empty working
tree and no `.gitignore` of its own — but if you instead try to commit
`dist/index.html` directly on the main branch, it will be silently
ignored unless you `git add -f`.

An equivalent, no-branch alternative: keep Pages source-free and instead
commit the built file to a tracked `docs/index.html` on `main` (again
bypassing `.gitignore` with `git add -f docs/index.html`, or by excluding
`docs/` from `.gitignore`), then set GitHub Pages to serve from
`main` / `docs`.

## Rebuilding a deployed dashboard

Any time the reporting window or repo data changes, rebuild and redeploy
the single file — there's no incremental deploy step:

```bash
python3 -m collector.collect
python3 site/build.py
# then re-copy/re-commit dist/index.html via whichever option above
```
