# Aurum AI

Aurum AI is a web-based XAU/USD analysis application.

## Deploy on Render

Create a new **Web Service** from this GitHub repository. Render detects `render.yaml`, installs the requirements, and runs `python app.py`.

The application listens on the host and port supplied by Render. The existing price API endpoints are intentionally preserved.

> Analysis tool only. It does not place trades automatically.
