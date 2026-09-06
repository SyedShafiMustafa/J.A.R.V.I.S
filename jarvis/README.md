# Jarvis UI

Minimal local frontend for Jarvis.

## Quick start

Run everything locally:

```bash
cd jarvis
python launcher.py
```

This starts:

- the backend API stub
- the Vite + React UI dev server
- opens the UI in your browser automatically

The UI is available at the URL printed by the launcher, usually:

- `http://127.0.0.1:5173`

Stop it with `Ctrl + C`.

## Frontend-only development

If you only want to work on the UI:

```bash
cd jarvis/ui
npm run dev
```

## Project structure

```
jarvis/
├── backend/
│   └── api.py        # minimal backend API stub for the UI
├── ui/
│   ├── package.json
│   ├── vite.config.js
│   ├── index.html
│   └── src/
│       ├── main.jsx
│       ├── App.jsx
│       └── api.js
├── launcher.py       # starts backend + UI together
└── README.md
```

## What this is

Right now this is a frontend skeleton, not a full application.

It includes:

- a Vite + React dev setup
- a minimal status UI
- an `api.js` module prepared for backend calls
- a backend proxy so `/api/*` requests can go to the backend during development

It is intentionally bare so the UI can be redesigned later without
untangling a large existing interface.

## Backend proxy

The Vite dev server proxies requests from `/api` to the backend API
stub running locally.

That means the frontend can call:

- `/api/health`
- `/api/transcript`
- `/api/command`

without hardcoding the backend host in the UI code.

## What to build next

This skeleton gives you a working starting point. The next step is usually:

- decide on the actual UI state you want to show
- connect the UI to real backend behavior
- add the screens, flows, and interactions you want

`api.js` is the place to grow the frontend/backend contract as the
backend becomes more complete.
