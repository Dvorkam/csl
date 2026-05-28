# static/js

## htmx.min.js

HTMX **2.0.4** — vendored, no CDN dependency at runtime.

Ships inside the PyPI wheel via `[tool.setuptools.package-data]`. Users need no manual steps.

To upgrade:

```bash
VERSION=2.0.4
curl -sL "https://unpkg.com/htmx.org@${VERSION}/dist/htmx.min.js" \
     -o control_station_lite/server/static/js/htmx.min.js
```

Update the version string above and in `base.html` (`<script src="/static/js/htmx.min.js">`).
