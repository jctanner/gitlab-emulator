# highlight.js (vendored)

Syntax highlighting for the file viewer, served locally so the UI does not
depend on a CDN.

- Source: `@highlightjs/cdn-assets` 11.12.0 (BSD-3-Clause, see `LICENSE`)
- `highlight.min.js`: the "common" build (core plus 36 languages)
- `dockerfile.min.js`: extra language, loaded after the core
- `github.min.css`: the GitHub light theme

To update, `npm pack @highlightjs/cdn-assets@<version>` and copy the same
files from `package/`, `package/languages/` and `package/styles/`.
