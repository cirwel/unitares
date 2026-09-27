# Dashboard extensions

The dashboard ships the tabs every UNITARES install can read: Overview, Agents,
Discoveries, Dialectic, Activity, EISV, Risk and Security. Anything more
specific to a deployment belongs to that deployment: a panel for its own
residents, a census of its scheduled jobs, a research instrument. It is added
as an **extension**, which lives outside this repository and is loaded only by
the server that points at it.

An install that configures nothing gets the core tabs and nothing else, so no
operator inherits another operator's customisations.

## Turning it on

Set one variable on the governance server:

```
UNITARES_DASHBOARD_EXT_DIR=/path/to/your-dashboard-ext
```

The server serves that directory at `/dashboard/ext/`, reading each file on
every request, so an edit is live on the next page load without a restart.
It serves only `.js`, `.json` and `.css`, never outside the directory, and
only to an authenticated caller: the same bearer or passkey session that the
data routes accept. Without the variable the route answers 404.

## The directory

```
your-dashboard-ext/
  manifest.json
  data-ext.js            # optional: accessors your sections call
  sections/
    queue.js
```

`manifest.json`:

```json
{
  "scripts": ["data-ext.js"],
  "sections": [
    {
      "id": "queue",
      "label": "Queue",
      "title": "Review queue",
      "global": "Queue",
      "script": "sections/queue.js",
      "mount": "queue-mount",
      "auto": false
    }
  ]
}
```

| field | required | meaning |
|---|---|---|
| `scripts` | no | Loaded first, in order. Use them for shared accessors. |
| `id` | yes | Nav hash (`#queue`). Lowercase letters, digits and `-`, starting with a letter. |
| `label` | no | Nav text. Defaults to `id`. |
| `title` | no | Heading rendered above the mount. |
| `global` | yes | The `window` name your script assigns, e.g. `window.Queue`. |
| `script` | yes | Path inside the directory. |
| `mount` | no | The id of the `<div>` your section renders into. Defaults to `<id>-mount`. |
| `auto` | no | `true` puts the tab on the live refresh (the 10s poll while the event stream is down, the event doorbell while it is up). Leave it off for anything that reads a daily or expensive aggregate. |

The loader skips an entry whose `id` or `global` is malformed, whose script
fails to load, or whose `id` matches a core tab. Skipping is silent to the
viewer and logged to the console, so test the manifest itself (see below).

## Writing a section

A section follows the same contract as a core one (`skills/unitares-dashboard`
has the full conventions):

```js
(function () {
  "use strict";
  async function load() {
    const { source, data } = await DATA.queueItems();
    document.getElementById("queue-mount").innerHTML =
      `<span class="src-badge ${source}">${source}</span> ${data.length} waiting`;
  }
  window.Queue = { load };            // add retheme() if you draw charts
})();
```

- `load()` runs when the tab is first opened and, with `auto`, on each refresh.
  Update in place on later calls rather than rebuilding inputs the viewer is using.
- `retheme()` (optional) is called when the viewer toggles ink/paper.
- Style with the classes and tokens already in `kit.css` and `tokens.css`.

## Reading data

Views never call `fetch` directly. Add accessors in a `scripts` file on top of
`DATA.seam`, so an extension authenticates and degrades exactly as the core
does:

```js
(function () {
  const { authFetch, callTool, withFallback } = window.DATA.seam;
  Object.assign(window.DATA, {
    async queueItems() {
      return withFallback(
        async () => { const j = await authFetch("/v1/your/route"); return j && Array.isArray(j.items) ? j.items : null; },
        () => [],   // what the view renders when the live read fails
      );
    },
  });
})();
```

- `authFetch(path, opts)` sends the bearer and the session cookie, and sends a
  cookieless 401 to sign-in.
- `callTool(name, args)` calls a governance tool through `/v1/tools/call`.
- `withFallback(liveFn, emptyFn)` returns `{ source: "live", data }`, or
  `{ source: "snapshot", data: emptyFn() }` when `liveFn` throws or returns
  `null`. Make the fallback an empty shape your view can render. The bundled
  snapshot covers only the core tabs.

## Trust

Extension code runs in the page with the page's full privileges, including the
viewer's session. It is your own code, served from your own disk to people you
have already authenticated. Keep the directory writable only by the operator
account, as you would the server's config.

## Testing

The core loader is deliberately quiet about a bad entry, so test the manifest:
that every named file exists, that each script defines its `global` with a
`load` function, and that every `DATA.x(` a section calls is defined by core
`data.js` or your scripts. Evaluate core `redesign/data.js` before your own
scripts in a jsdom context, in the same order `app.html` loads them.
`dashboard/tests/app-extensions.test.js` shows the loader's own contract.
