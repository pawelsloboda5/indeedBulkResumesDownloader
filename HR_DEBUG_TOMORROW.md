# Tomorrow's session — what to ask HR to do

We need two pieces of ground truth from her live US Indeed Employer dashboard. Both are 30-second tasks in Chrome DevTools. Walk her through them on the call.

---

## Snippet 1 — Find the Download button (fixes Option 2)

**Goal:** identify the exact HTML of Indeed's "Download resume" button so we can patch the selector.

**Walk her through:**

1. Have her open Indeed Employer in Chrome and navigate to a candidate's profile (any candidate, the one she's looking at is fine).
2. Press **F12** (or right-click → **Inspect**) → click the **Console** tab at the top.
3. Paste the entire snippet below into the Console and press Enter:

```javascript
console.log("=== Indeed Download Button Candidates ===");
const candidates = [...document.querySelectorAll(
  'button, a, [role="button"], [data-testid*="download" i], [aria-label*="download" i]'
)];
const matches = candidates.filter(el => {
  const t = (el.textContent || '').toLowerCase();
  const a = (el.getAttribute('aria-label') || '').toLowerCase();
  const d = (el.getAttribute('data-testid') || '').toLowerCase();
  const h = (el.getAttribute('href') || '').toLowerCase();
  return /(download|resume|cv|télécharger|export)/.test(t + ' ' + a + ' ' + d) ||
         /resume|download/.test(h);
});
if (!matches.length) {
  console.log('NO MATCHES — button may be inside an iframe or behind a Resume tab. Click the Resume tab and re-run.');
} else {
  matches.forEach((el, i) => {
    console.log(`\n--- candidate ${i+1} (${el.tagName.toLowerCase()}) ---`);
    console.log('text:', JSON.stringify(el.textContent.trim().slice(0, 80)));
    console.log('aria-label:', el.getAttribute('aria-label'));
    console.log('data-testid:', el.getAttribute('data-testid'));
    console.log('href:', el.getAttribute('href'));
    console.log('outerHTML:', el.outerHTML.slice(0, 400));
  });
}
```

4. **Screenshot the whole console output** and send it to you. That's it.

If she sees `NO MATCHES`, she should click into the candidate's **Resume** tab (sometimes Indeed gates it behind a tab) and re-run.

---

## Snippet 2 — Capture the GraphQL request (fixes Option 1)

**Goal:** see the actual `operationName`, `variables`, and headers that Indeed's own dashboard sends, so we can match them in the code.

**Walk her through:**

1. Same Indeed tab. Press **F12** → click the **Network** tab → in the filter box type `graphql`.
2. Click the 🔴 record button if it isn't already red (it usually is by default).
3. Now have her **click on a candidate, change the sort dropdown, or refresh the page** — anything that makes the dashboard load candidate data.
4. In the Network panel, **graphql requests will appear**. They're POSTs to `apis.indeed.com/graphql`. There may be 2–10 of them.
5. For each one, **right-click → Copy → Copy as cURL (bash)** and paste them into a text file. Send the file.

If "Copy as cURL" feels intimidating, the easier version:
1. Click any graphql row in the Network list.
2. The right panel shows tabs: **Headers**, **Payload**, **Response**.
3. Take screenshots of:
   - The **Headers** tab (all of it — request URL + request headers)
   - The **Payload** tab (especially the `operationName` and `variables` fields)
   - The first 20 lines of the **Response** tab
4. Send the screenshots.

If the easier version is still too much, paste this into the **Console** tab instead (after she does steps 1–2 above):

```javascript
const orig = window.fetch;
window.__captured = [];
window.fetch = function(...args) {
  if (String(args[0]).includes('graphql')) {
    try {
      const body = args[1] && args[1].body ? JSON.parse(args[1].body) : null;
      const captured = {
        url: args[0],
        operationName: body && body.operationName,
        variables: body && body.variables,
        headers: args[1] && args[1].headers,
      };
      window.__captured.push(captured);
      console.log('[GQL]', captured.operationName, captured.variables);
    } catch (e) {}
  }
  return orig.apply(this, args);
};
console.log('GraphQL monitor on. Click around the candidate list, then run: copy(JSON.stringify(window.__captured, null, 2)) — that puts everything on your clipboard.');
```

She clicks around for ~30 sec, then runs `copy(JSON.stringify(window.__captured, null, 2))` in the console — that copies all captured requests to clipboard, she pastes into a message.

---

## What we'll do with the data

**From snippet 1 (button HTML):** I patch the selector list at `indeed_downloader.py:1057-1075` with the exact tag/attributes she sends. One line, then a CI rebuild (~5 min), and she retries Option 2 — should download cleanly this time.

**From snippet 2 (GraphQL):** Compare what her dashboard actually sends vs. what the code sends. We're specifically looking for differences in:
- `operationName` (currently `FindRCPMatches`)
- `indeed-client-sub-app` header value (currently `talent-organization-modules`)
- `indeed-client-sub-app-component` header value (currently `./CandidateListPage`)
- Any extra headers we're missing
- Variable shape inside `input` (e.g., `jobIdentifiers` vs. something else)

I update the code to match, rebuild, retest. That fixes Option 1 (10x speedup over Option 2 for her ~1000 candidates/run cadence).

---

## What's already shipped

The next CI build (latest commit on `main`) includes:

- **Wide-net download-button selector chain** — 11 different XPath patterns covering all the common Indeed variants. Real chance Option 2 works even before tomorrow's diagnostic.
- **Application-data automation (Frontend mode)** — the tool now also opens the "..." menu per candidate, clicks "Download application data", ticks HTML + JSON (skipping redundant PDF), and clicks "Download files". Files land in per-candidate subfolders: `downloads/<Job>/<Candidate>/{resume.pdf, application.html, application.json}`.
- **Menu toggle** — a 4th question ("Also download application data? 1=yes 2=no") appears after the status filter. Defaults to yes.
- **Separate dedup** — rerunning skips candidates already downloaded; CV and app-data tracked independently so she can fill in app-data for a batch where CVs already landed.

---

## Snippet 3 — Capture the "Download application data" endpoint (fixes Option 1 for app data)

**Goal:** see the actual network call(s) Indeed fires when HR clicks "Download files" in the application-data modal. We need this to implement Backend-mode app-data download (10× faster than Frontend for ~1000 candidates/run).

**Walk her through:**

1. On a candidate profile, **F12 → Console** tab.
2. Paste and run:

   ```javascript
   (function(){
     const cap = window.__appDataCalls = [];
     const origFetch = window.fetch;
     window.fetch = function(...args){
       const u = String(args[0] || '');
       if (/indeed\.com|application|cao_post|original-application/i.test(u)) {
         cap.push({
           tool: 'fetch',
           url: u,
           opts: args[1] ? {method: args[1].method, body: args[1].body, headers: args[1].headers} : {}
         });
         console.log('[fetch]', u);
       }
       return origFetch.apply(this, args);
     };
     const origOpen = XMLHttpRequest.prototype.open;
     const origSend = XMLHttpRequest.prototype.send;
     XMLHttpRequest.prototype.open = function(m,u){ this._u=u; this._m=m; return origOpen.apply(this, arguments); };
     XMLHttpRequest.prototype.send = function(b){
       if (this._u && /indeed\.com|application|cao_post|original-application/i.test(this._u)) {
         cap.push({tool:'xhr', method: this._m, url: this._u, body: b});
         console.log('[xhr]', this._m, this._u);
       }
       return origSend.apply(this, arguments);
     };
     console.log('App-data monitor live. Click "..." → Download application data → tick HTML + JSON → Download files. Then run: copy(JSON.stringify(__appDataCalls,null,2))');
   })();
   ```

3. Without closing DevTools, she clicks the **"..."** button on the candidate → **Download application data** → tick HTML + JSON (skip PDF) → **Download files**.
4. Back in the Console, she runs:

   ```javascript
   copy(JSON.stringify(__appDataCalls, null, 2))
   ```

   That puts everything on her clipboard. She pastes it into a message to you.

## What we do with the app-data capture

The JSON dump will show the endpoint Indeed hits (probably a POST to `employers.indeed.com/api/...` with a body that lists which files to pull, returning presigned URLs for the file downloads). We write a Backend-mode sibling to `_download_application_data_frontend` that calls the endpoint directly and streams both files straight to disk — no clicking. At ~10× the speed of the Selenium flow for a 1000-candidate run, this is the main reason to bother.
