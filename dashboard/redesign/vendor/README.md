# Vendored third-party scripts

The dashboard pages load these libraries from this directory, on the
governance origin, instead of from a CDN. Each file is byte-identical to the
file of the same name in the package's npm release tarball, and its upstream
licence file sits beside it.

| Library | Version | File (from the npm tarball) | Licence | Loaded by |
|---|---|---|---|---|
| Chart.js | 4.4.0 | `package/dist/chart.umd.js` | MIT (`chart.js-4.4.0/LICENSE.md`) | `redesign/app.html` |
| d3 | 7.9.0 | `package/dist/d3.min.js` | ISC (`d3-7.9.0/LICENSE`) | `phase.html` |

`tests/test_dashboard_vendored_scripts.py` pins the SHA-512 of every file here
and fails if a dashboard page loads a script from another origin.

## Updating a library

1. Read `dist.integrity` for the exact version from
   `https://registry.npmjs.org/<package>/<version>`.
2. Download the tarball named in `dist.tarball` and check that its SHA-512
   (base64) matches `dist.integrity`.
3. Download the same file from at least two independent CDNs (for example
   `cdn.jsdelivr.net/npm/<package>@<version>/<path>` and
   `unpkg.com/<package>@<version>/<path>`) and check that both are
   byte-identical to the file in the tarball. Fetch the path the tarball
   contains: jsDelivr generates a `.min.js` it does not have, with its own
   banner prepended.
4. Copy the file and the package's licence file into a new
   `<package>-<version>/` directory, point the page at it, update the pins in
   the test, and delete the old directory.
