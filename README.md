# mytube

A minimal FastAPI web application that exposes a single "remote control" for casting a YouTube recommendation to a Chromecast device.

## Running the app

This project uses the [uv](https://github.com/astral-sh/uv) package manager. To launch the development server and cast controller, run:

```bash
uv run mytube
```

By default the server listens on `0.0.0.0:8000`. You can override the host or port:

```bash
uv run mytube -- --host 127.0.0.1 --port 5000
```

Navigating to the root page presents a single link. Clicking the link will request that the YouTube video `CYlon2tvywA` be played on the first Chromecast discovered on your local network.
