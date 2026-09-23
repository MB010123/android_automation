"""Swagger UI / ReDoc HTML for the VPS backend (no extra Python dependencies)."""
from __future__ import annotations

OPENAPI_JSON_PATH = "/openapi.json"
SWAGGER_UI_PATH = "/docs"
REDOC_PATH = "/redoc"


def swagger_ui_html(*, openapi_url: str = OPENAPI_JSON_PATH) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Mobi-Rent VPS API</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5.11.0/swagger-ui.css"/>
</head>
<body>
<div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5.11.0/swagger-ui-bundle.js"></script>
<script>
window.onload = function() {{
  SwaggerUIBundle({{
    url: {openapi_url!r},
    dom_id: '#swagger-ui',
    deepLinking: true,
    persistAuthorization: false,
    tryItOutEnabled: true
  }});
}};
</script>
</body>
</html>"""


def redoc_html(*, openapi_url: str = OPENAPI_JSON_PATH) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Mobi-Rent VPS API — ReDoc</title>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>body {{ margin: 0; padding: 0; }}</style>
</head>
<body>
  <redoc spec-url="{openapi_url}"></redoc>
  <script src="https://cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js"></script>
</body>
</html>"""
